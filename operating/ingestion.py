"""Continuous live ingestion with completeness tracking (scheduler-free entry point).

One ``run`` advances a durable cursor per configured source through the
existing GET-only readers only (no HTTP code here):

* ``portal_relation`` -- ``integrations.source_mirror.SourceMirror.refresh``
  (the only Portal Workbench API reader). The mirror owns the cursor (its
  ``since`` watermark) and commits each relation capture atomically; this
  module records the watermark/capture before and after.
* ``mail_folder`` -- ``integrations.graph_read.GraphMailReader.list_messages``
  oldest-first from ``received high-water - overlap``. A capped read still
  advances the cursor to the newest message it returned (every older message
  in the window was returned before it), so a backlog drains over runs.
  The first run (no cursor) starts at ``start_at`` or ``initial_window_days``
  before its anchor: the run's clock (default) or, with
  ``initial_window_anchor: newest_message``, the folder's newest message (one
  newest-first metadata GET), so a folder with no recent mail still has its
  last window read instead of an empty one.
  Mail completeness is by received time only: ``complete`` means every message
  with ``receivedDateTime >= cursor - overlap`` was read. A message that enters
  the folder later with an older received time (moved back from Junk or
  another folder, or delivered later than the overlap) is never read, and
  edits/moves/deletions of ingested messages are not tracked, so a complete
  mail unit is never evidence that a message does not exist. The cursor is
  composite (received second + ids read at that second); after a
  ``cursor_stalled`` run (more messages in the overlap window than the cap) the
  next run reads the overlap window again with a widened cap (2,500), so a late
  message received inside the window is still read; only if even that read
  ends inside the window does it continue from the high-water second and
  report ``overlap_window_unread``. The unread part of the window is kept in
  the cursor (``overlap_gap``); every later run reads it once with the
  widened cap and keeps reporting ``overlap_window_unread`` until a read
  reaches its end. Every read of a run counts in ``pages``/``items_read``.
* ``sharepoint_folder`` -- ``integrations.graph_read.SharePointReader.list_folder``
  breadth-first from a configured folder (e.g. QCQA ``/AOs/2026 - AOs``). The
  cursor is the traversal frontier, committed after every folder, so a crash
  resumes with the next unlisted folder. Queued folders carry their item id:
  a renamed/moved folder is re-resolved by id and followed only while it stays
  at or under the configured path (else dropped, ``folder_moved_out``); one
  that can no longer be seen is dropped (``folder_not_visible``: deleted, moved
  out of the grant or hidden by an audience/ACL change, which the reader does
  not distinguish) and one that fails three runs in a row is dropped
  (``folder_failed:<Class>``), which ends that traversal incomplete without
  marking anything absent, and the next run starts a new traversal.
* ``bc_entity_set`` -- ``integrations.bc_read.BusinessCentralReader.list_entities``
  per company. The closed BC grammar has no modified-since filter, so every
  run is a bounded full scan; the cursor is the scan counter and the
  ``lastModifiedDateTime`` high-water.
* ``mc_sources`` -- ``integrations.mc_sources.McCaptureReader``: one Mission Control
  operator scope (MC read tools only) captured into ``<state>/mc-capture/capture.sqlite``,
  one unit per collection (``<id>.tasks``, ``.approvals``, ``.task_events``,
  ``.buckets``, ``.knowledge``) with the capture's own state as its reasons; the capture store
  keeps the cursors (task details resume after the last task read). Records that no
  principal granted the collection may read for want of their MC project grant add
  ``withheld:project_grant:<n>`` (``not_measured`` when the audience cannot tell).

Completeness (ARCHITECTURE-REVIEW F8): every unit of every run records pages,
items, new/changed/unchanged, the exact reasons it is incomplete
(``cap:item_cap``, ``cap:page_cap``, ``cap:portal_row_bound``,
``source_error:<ExceptionClass>[:<detail>]`` (detail from the guard's fixed
vocabulary: ``http_429``, ``timeout``, ``retried.http_503`` ...),
``not_visible``, ``blocked:people_exclusion:<code>`` (the people guard refused
the mailbox before any request for want of its ID list: later_ask
``excluded-people-oids``, also the run report's ``blocked_upstream``; a guard whose
directory listing failed reports that failure), ``traversal_in_progress``,
``cursor_stalled``, ...), the cursor before and after, and lag against the
source high-water mark where the source exposes one. A unit is ``complete``
only when it has no reason at all; a cap, truncation or error is never
reported as complete. Items withheld from the ingest principal by audience or
source ACL add ``withheld:<n>``: they are inaccessible, not absent. Absence is
recorded (``absent_since_run``) only after a clean full traversal/scan (no
reason at all) and is never a deletion proof.

Ingest-store rows were read for the ingest principal; they are not servable to
an employee without resolving each citation for that employee
(``CaseRuntime.resolve_citation``).

Durable state is our own isolated SQLite store ``<state>/ingestion/ingest.sqlite``
(items, cursors, runs, per-unit results); Portal rows stay in the case
runtime's mirror file. Item rows and cursors are committed in one transaction
per unit (per folder for SharePoint); a run that dies leaves its run row
``running`` and the next run marks it ``interrupted``.

``status`` is counts-only (numbers, booleans, enum reasons and configured
source ids): no subjects, names, ids, citations or cursor values.

CLI (credentialed live verifier; prints counts only)::

    python3 -m operating.ingestion run --state-dir D --sources F \\
        --live-audience A --mirror-audience M [--runs 2]
    python3 -m operating.ingestion status --state-dir D [--sources F]
    python3 -m operating.ingestion validate --sources F

A ``sharepoint_folder`` source names either a ``drive_id`` or a ``site`` (a
site URL name, ``/sites/<site>`` on the host of ``SHAREPOINT_SITE_ID``): a site
is resolved to its default document library at run time through the GET-only
Graph client (``resolve_sites``), and live-audience keys name it as
``sharepoint:${site:<site>}:<path>``. The AGENTS site's ``/Ontology UseCases``
folder (Vince's delivered use cases) is such a unit; its new/changed files
are taken in by ``python3 -m operating.use_case_intake`` (originals pinned by
SHA-256, extraction, evaluation-set stubs; our own state only).

Scheduled capture cycles are the host's (``python3 -m operating.host``, live check
``host.cycles``); this module stays for the units its consumers still read (the
use-case intake, the case runtime's mirror and ``graph.mail_paging``).
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

from ontology.core import LEVELS, Actor


SOURCES_SCHEMA = "plx-ingest-sources/v1"
STATUS_SCHEMA = "plx-ingest-status/v1"
RUN_SCHEMA = "plx-ingest-run/v1"
STORE_DIR = "ingestion"
STORE_FILE = "ingest.sqlite"
KINDS = ("portal_relation", "mail_folder", "sharepoint_folder", "bc_entity_set",
         "mc_sources")
# What "complete" means per kind (also shown in status for case-facing surfaces).
COMPLETENESS_BASIS = {"portal_relation": "mirror_since_watermark_not_atomic",
                      "mail_folder": "received_time_window_not_absence",
                      "sharepoint_folder": "full_traversal",
                      "bc_entity_set": "full_scan",
                      "mc_sources": "operator_scope_read_not_atomic"}
_UTC = timezone.utc
_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_RELATION = re.compile(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")
_ISO_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_INSTANT = re.compile(r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?"
                      r"(Z|[+-]\d{2}:\d{2})?$")
_GUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                   r"[0-9a-fA-F]{12}$")
_ENV_REF = re.compile(r"^\$\{([A-Z][A-Z0-9_]{0,80})\}$")
_SECRET_NAME = re.compile(r"KEY|SECRET|TOKEN|PASSWORD|PASSWD|COOKIE|DSN|CREDENTIAL|SESSION")
STALL_RECOVERY_ITEMS = 2500  # 50 pages x 50 messages, the reader's widest mail read
# Where a mail unit's first window ends: the run's clock, or the newest message
# the folder holds (one newest-first metadata GET), so a quiet folder's first
# run still reads its last ``initial_window_days`` of mail.
MAIL_WINDOW_ANCHORS = ("now", "newest_message")
FOLDER_FAILURE_LIMIT = 3  # consecutive failed runs before a queued subfolder is dropped
_REASON = re.compile(r"^[a-z_]+(?::[A-Za-z0-9_.]+){0,2}$")
REASON_MAX = 64  # devloop.live_verify.ENUM_STRING: longer reasons are hidden there
PEOPLE_BLOCK = "blocked:people_exclusion:"  # + people_exclusion.CODES entry
_DETAIL = re.compile(r"^[a-z0-9_]+(?:\.[a-z0-9_]+)?$")


class IngestError(ValueError):
    """A configuration or store error (code only, never source content)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# ------------------------------------------------------------------ helpers

def _now_iso(moment: datetime) -> str:
    return moment.astimezone(_UTC).isoformat()


def _instant(value: Any) -> datetime | None:
    """A source timestamp (Graph/BC/mirror ISO forms); zone-less reads as UTC."""
    if not isinstance(value, str):
        return None
    match = _INSTANT.fullmatch(value.strip())
    if match is None:
        return None
    fraction = (match[3] or "0")[:6].ljust(6, "0")
    zone = match[4]
    zone = "+00:00" if zone in (None, "Z") else zone
    try:
        return datetime.fromisoformat(f"{match[1]}T{match[2]}.{fraction}{zone}").astimezone(_UTC)
    except ValueError:
        return None


def _graph_since(moment: datetime) -> str:
    return moment.astimezone(_UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      default=str)


def _digest(value: Any) -> str:
    return sha256(_canonical(value).encode()).hexdigest()


def _error_reason(exc: BaseException) -> str:
    """The exact error class of a failed source read, never its message.

    A ``SourceUnavailable`` also carries a bounded diagnosis token from the
    guard's fixed vocabulary (``live_source_guard.unavailable_detail``:
    ``http_429``, ``timeout``, ``nextlink_escaped``,
    ``retried.http_503`` ...), so an operator can tell a throttle from an
    outage; source text never reaches the reason. The whole reason stays
    within ``REASON_MAX`` (the live verifier's enum bound): a subclass whose
    name would overflow it is reported by its base class ``SourceUnavailable``."""
    from integrations.live_source_guard import (NotVisible, ReadOnlyGuardError,
                                                unavailable_detail)
    from integrations.people_exclusion import ExclusionUnavailable
    from integrations.source_mirror import MirrorError

    if isinstance(exc, ReadOnlyGuardError):
        return "guard_refused:ReadOnlyGuardError"
    if isinstance(exc, ExclusionUnavailable):
        from integrations.graph_read import exclusion_later_ask

        if exclusion_later_ask(exc):
            return f"{PEOPLE_BLOCK}{exc.code}"
        # unresolved: the directory listing failed (its own failure) or was truncated
        return (_error_reason(exc.__cause__) if exc.__cause__ is not None
                else f"source_error:ExclusionUnavailable:{exc.code}")
    if isinstance(exc, NotVisible):
        return "not_visible"
    if isinstance(exc, MirrorError):
        return _mirror_reason(f"{exc.code}: {exc}")
    reason = f"source_error:{type(exc).__name__}"
    detail = unavailable_detail(exc)
    if detail is not None and _DETAIL.fullmatch(detail):
        if len(f"{reason}:{detail}") > REASON_MAX:
            reason = "source_error:SourceUnavailable"
        reason += ":" + detail
    return reason


def blocked_upstream(units: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The run's typed blocks: each unit the people guard refused before any request
    (reason ``blocked:people_exclusion:<code>``) as ``PeopleExclusion.block_part`` names
    it (later_ask ``excluded-people-oids``; codes only, never an id)."""
    from integrations.people_exclusion import CODES, PeopleExclusion

    out = []
    for unit in units:
        for reason in unit.get("reasons") or ():
            code = reason[len(PEOPLE_BLOCK):] if reason.startswith(PEOPLE_BLOCK) else None
            if code in CODES:
                out.append(PeopleExclusion(unavailable=code).block_part(unit["unit_id"]))
    return out


def _mirror_reason(error: str) -> str:
    """Classify a mirror refresh error (``"<code>: <message>"``) into one reason.

    The Workbench client raises on its bounds (``integrations.workbench_read_api``:
    "relation row bound reached", "page bound reached"); those are caps, not
    outages, and the relation stays at its last committed capture."""
    code = error.split(":", 1)[0].strip() if isinstance(error, str) else ""
    text = error.lower() if isinstance(error, str) else ""
    if "row bound" in text:
        return "cap:portal_row_bound"
    if "page bound" in text:
        return "cap:portal_page_bound"
    if code == "not_visible":
        return "not_visible"
    code = code if re.fullmatch(r"[a-z_]{1,40}", code or "") else "unknown"
    return f"source_error:mirror.{code}"


def _bounded_int(raw: Mapping[str, Any], name: str, default: int, low: int, high: int) -> int:
    value = raw.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise IngestError(f"ingest_source_{name}_invalid")
    return value


# ------------------------------------------------------------------ configuration

def _substitute(value: Any, environ: Mapping[str, str], missing: set[str]) -> Any:
    """``"${NAME}"`` (whole string) -> the variable's value; credential names refused."""
    if isinstance(value, str):
        match = _ENV_REF.fullmatch(value)
        if match is None:
            return value
        name = match[1]
        if _SECRET_NAME.search(name):
            raise IngestError("ingest_config_secret_reference_refused")
        resolved = (environ.get(name) or "").strip()
        if not resolved:
            missing.add(name)
            return None
        return resolved
    if isinstance(value, list):
        return [_substitute(item, environ, missing) for item in value]
    if isinstance(value, dict):
        return {key: _substitute(item, environ, missing) for key, item in value.items()}
    return value


def _source(raw: Any, environ: Mapping[str, str]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise IngestError("ingest_source_invalid")
    source_id, kind = raw.get("id"), raw.get("kind")
    if not isinstance(source_id, str) or _ID.fullmatch(source_id) is None:
        raise IngestError("ingest_source_id_invalid")
    if kind not in KINDS:
        raise IngestError("ingest_source_kind_invalid")
    missing: set[str] = set()
    raw = _substitute(raw, environ, missing)
    spec: dict[str, Any] = {"id": source_id, "kind": kind,
                            "not_configured": sorted(missing)}
    allowed = {"id", "kind", "note"}
    if kind == "portal_relation":
        allowed |= {"relation"}
        if not isinstance(raw.get("relation"), str) or not _RELATION.fullmatch(raw["relation"]):
            raise IngestError("ingest_source_relation_invalid")
        spec["relation"] = raw["relation"]
    elif kind == "mail_folder":
        allowed |= {"mailbox", "folder", "start_at", "initial_window_days",
                    "initial_window_anchor", "overlap_seconds", "page_size", "max_pages",
                    "max_items"}
        mailbox = raw.get("mailbox")
        if not missing and (not isinstance(mailbox, str) or not 3 <= len(mailbox) <= 320):
            raise IngestError("ingest_source_mailbox_invalid")
        folder = raw.get("folder", "inbox")
        if not isinstance(folder, str) or not 1 <= len(folder) <= 200:
            raise IngestError("ingest_source_folder_invalid")
        start_at = raw.get("start_at")
        if start_at is not None and (not isinstance(start_at, str)
                                     or _ISO_Z.fullmatch(start_at) is None):
            raise IngestError("ingest_source_start_at_invalid")
        anchor = raw.get("initial_window_anchor", "now")
        if anchor not in MAIL_WINDOW_ANCHORS or (start_at is not None and anchor != "now"):
            raise IngestError("ingest_source_initial_window_anchor_invalid")
        spec.update(mailbox=mailbox, folder=folder, start_at=start_at,
                    initial_window_days=_bounded_int(raw, "initial_window_days", 30, 1, 3650),
                    overlap_seconds=_bounded_int(raw, "overlap_seconds", 900, 0, 86400),
                    page_size=_bounded_int(raw, "page_size", 50, 1, 50),
                    max_pages=_bounded_int(raw, "max_pages", 10, 1, 50),
                    max_items=_bounded_int(raw, "max_items", 500, 1, 5000))
        if anchor != "now":
            # Only a non-default anchor enters the spec, so the config digest
            # (and every persisted cursor) of an existing unit is unchanged.
            spec["initial_window_anchor"] = anchor
    elif kind == "sharepoint_folder":
        allowed |= {"drive_id", "site", "path", "recursive", "max_depth",
                    "max_folders_per_run", "page_size", "max_pages", "max_items"}
        drive, site = raw.get("drive_id"), raw.get("site")
        if site is not None:
            # A site URL name (``/sites/<site>``): its default document library is
            # resolved at run time (``resolve_sites``) on the configured site's
            # host, never a drive id or host written into the config.
            from integrations import live_source_grammar as grammar
            from integrations.live_source_guard import sharepoint_host_from_env

            if "drive_id" in raw or not grammar.TYPES["site_name"].valid(site):
                raise IngestError("ingest_source_site_invalid")
            if sharepoint_host_from_env(environ) is None:
                missing.add("SHAREPOINT_SITE_ID")
            spec["not_configured"] = sorted(missing)
            spec["site"] = site
        elif not missing and (not isinstance(drive, str) or not 1 <= len(drive) <= 200):
            raise IngestError("ingest_source_drive_invalid")
        path = raw.get("path", "/")
        if not isinstance(path, str) or not path.startswith("/") or len(path) > 1024:
            raise IngestError("ingest_source_path_invalid")
        recursive = raw.get("recursive", False)
        if not isinstance(recursive, bool):
            raise IngestError("ingest_source_recursive_invalid")
        spec.update(drive_id=drive, path=path, recursive=recursive,
                    max_depth=_bounded_int(raw, "max_depth", 5, 0, 20),
                    max_folders_per_run=_bounded_int(raw, "max_folders_per_run", 50, 1, 500),
                    page_size=_bounded_int(raw, "page_size", 200, 1, 200),
                    max_pages=_bounded_int(raw, "max_pages", 5, 1, 50),
                    max_items=_bounded_int(raw, "max_items", 1000, 1, 5000))
    elif kind == "mc_sources":
        from integrations.mc_sources import CaptureSpec

        fields = ("scope", "knowledge_queries", "knowledge_limit", "max_task_reads",
                  "page_size")
        allowed |= set(fields)
        options = {k: raw[k] for k in fields if k in raw}
        if isinstance(options.get("knowledge_queries"), list):
            options["knowledge_queries"] = tuple(options["knowledge_queries"])
        try:
            CaptureSpec(**options)
        except (TypeError, ValueError):
            raise IngestError("ingest_source_mc_invalid") from None
        spec.update({k: list(v) if isinstance(v, tuple) else v for k, v in options.items()})
    else:
        allowed |= {"entity_set", "companies", "page_size", "max_pages", "max_items"}
        from integrations import live_source_grammar as grammar

        entity_set = raw.get("entity_set")
        if not grammar.TYPES["entity_set"].valid(entity_set):
            raise IngestError("ingest_source_entity_set_invalid")
        companies = raw.get("companies", "all")
        if companies != "all" and (not isinstance(companies, list) or not companies or any(
                not isinstance(c, str) or _GUID.fullmatch(c) is None for c in companies)):
            raise IngestError("ingest_source_companies_invalid")
        spec.update(entity_set=entity_set,
                    companies="all" if companies == "all" else [c.lower() for c in companies],
                    page_size=_bounded_int(raw, "page_size", 500, 1, 1000),
                    max_pages=_bounded_int(raw, "max_pages", 10, 1, 50),
                    max_items=_bounded_int(raw, "max_items", 5000, 1, 5000))
    if set(raw) - allowed:
        raise IngestError("ingest_source_unknown_field")
    spec["config_digest"] = _digest({k: v for k, v in spec.items() if k != "not_configured"})
    return spec


def load_sources(document: Any, environ: Mapping[str, str] | None = None
                 ) -> tuple[str, Actor, list[dict[str, Any]]]:
    """(tenant, ingest actor, validated sources) from a ``plx-ingest-sources/v1`` document."""
    env = os.environ if environ is None else environ
    if (not isinstance(document, dict) or document.get("schema") != SOURCES_SCHEMA
            or set(document) - {"schema", "tenant", "actor", "sources", "note"}):
        raise IngestError("ingest_sources_document_invalid")
    tenant = document.get("tenant")
    if not isinstance(tenant, str) or not tenant.strip():
        raise IngestError("ingest_sources_tenant_invalid")
    actor_raw = document.get("actor")
    if (not isinstance(actor_raw, dict) or set(actor_raw) - {"principals", "clearance"}
            or not isinstance(actor_raw.get("principals"), list)
            or not actor_raw["principals"]
            or any(not isinstance(p, str) or not p or "*" in p for p in actor_raw["principals"])
            or actor_raw.get("clearance", "internal") not in LEVELS):
        raise IngestError("ingest_sources_actor_invalid")
    actor = Actor(tenant, frozenset(actor_raw["principals"]),
                  actor_raw.get("clearance", "internal"))
    raw_sources = document.get("sources")
    if not isinstance(raw_sources, list) or not raw_sources or len(raw_sources) > 200:
        raise IngestError("ingest_sources_list_invalid")
    sources = [_source(raw, env) for raw in raw_sources]
    if len({s["id"] for s in sources}) != len(sources):
        raise IngestError("ingest_source_id_duplicate")
    return tenant, actor, sources


def load_sources_file(path: str | Path, environ: Mapping[str, str] | None = None
                      ) -> tuple[str, Actor, list[dict[str, Any]]]:
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise IngestError("ingest_sources_file_unreadable") from None
    return load_sources(document, environ)


_SITE_REF = re.compile(r"\$\{site:([A-Za-z0-9][A-Za-z0-9_-]{0,127})\}")


def _audience_document(path: str | Path) -> dict[str, Any]:
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise IngestError("ingest_live_audience_unreadable") from None
    if not isinstance(document, dict):
        raise IngestError("ingest_live_audience_unreadable")
    return document


def live_audience_sites(path: str | Path) -> frozenset[str]:
    """Site names a live-audience file grants anything on (``${site:NAME}`` keys).

    ``resolve_sites`` sends no request for a site outside this set."""
    document = _audience_document(path)
    names: set[str] = set()
    for field in ("audiences", "classifications"):
        table = document.get(field)
        if isinstance(table, dict):
            for key in table:
                if isinstance(key, str):
                    names.update(_SITE_REF.findall(key))
    return frozenset(names)


def load_live_audience(path: str | Path, environ: Mapping[str, str] | None = None, *,
                       sites: Mapping[str, str] | None = None) -> Any:
    """A ``plx-live-source-audience/v1`` file with ``${NAME}`` substitution in keys
    (e.g. ``sharepoint:${SHAREPOINT_DRIVE_ID}:/*``); a key naming a missing
    variable is dropped (default deny). ``${site:NAME}`` in a key is the drive id
    ``resolve_sites`` resolved for that site (``sites``); a key naming a site
    that was not resolved is dropped too."""
    from integrations.live_source_guard import LiveSourceAudience

    env = os.environ if environ is None else environ
    resolved = dict(sites or {})
    document = _audience_document(path)
    ref = re.compile(r"\$\{([A-Z][A-Z0-9_]{0,80})\}")

    def keys(table: Any) -> Any:
        if not isinstance(table, dict):
            return table
        result = {}
        for key, value in table.items():
            names = ref.findall(key) if isinstance(key, str) else []
            site_names = _SITE_REF.findall(key) if isinstance(key, str) else []
            if any(_SECRET_NAME.search(name) for name in names):
                raise IngestError("ingest_config_secret_reference_refused")
            if any(not (env.get(name) or "").strip() for name in names):
                continue
            if any(not resolved.get(name) for name in site_names):
                continue
            if names:
                key = ref.sub(lambda m: env[m[1]].strip(), key)
            if site_names:
                key = _SITE_REF.sub(lambda m: resolved[m[1]], key)
            result[key] = value
        return result

    for field in ("audiences", "classifications"):
        if field in document:
            document[field] = keys(document[field])
    if isinstance(document.get("source_acl_required"), list):
        document["source_acl_required"] = list(keys(
            {k: True for k in document["source_acl_required"] if isinstance(k, str)}))
    return LiveSourceAudience.from_json(document)


def resolve_sites(sources: Sequence[dict[str, Any]], api: Any,
                  environ: Mapping[str, str] | None = None, *,
                  granted: frozenset[str] | set[str] = frozenset()) -> dict[str, str]:
    """Resolve every ``site`` SharePoint source to its site's default drive.

    Through the GET-only Graph client (``graph_read.resolve_site_drive``) on the
    host of ``SHAREPOINT_SITE_ID``. A resolved source gets ``drive_id`` (and a
    config digest that includes it, so a site whose library changes starts a
    fresh cursor). A site the live audience grants nothing on is not requested
    (``not_visible`` + ``site_unresolved:no_grant``); a missing site reads
    ``not_visible`` + ``site_unresolved:not_found``; any other failure keeps its
    error class plus ``site_unresolved:source_error``. Returns site -> drive id
    for ``load_live_audience(sites=...)``. Each site is requested once."""
    from integrations import graph_read
    from integrations.live_source_guard import NotVisible, SourceNotFound, sharepoint_host_from_env

    env = os.environ if environ is None else environ
    host = sharepoint_host_from_env(env)
    outcome: dict[str, str | list[str]] = {}
    for spec in sources:
        name = spec.get("site")
        if spec.get("kind") != "sharepoint_folder" or not name or spec.get("not_configured"):
            continue
        if name not in outcome:
            if name not in granted:
                outcome[name] = ["not_visible", "site_unresolved:no_grant"]
            elif host is None:  # pragma: no cover - _source marks it not_configured
                outcome[name] = ["not_configured:SHAREPOINT_SITE_ID"]
            else:
                try:
                    outcome[name] = graph_read.resolve_site_drive(api, host, name)
                except SourceNotFound:
                    outcome[name] = ["not_visible", "site_unresolved:not_found"]
                except NotVisible:
                    outcome[name] = ["not_visible", "site_unresolved:not_visible"]
                except Exception as exc:  # noqa: BLE001 - recorded by class, never text
                    outcome[name] = [_error_reason(exc), "site_unresolved:source_error"]
        result = outcome[name]
        if isinstance(result, str):
            spec["drive_id"] = result
            spec.pop("site_unresolved", None)
            spec["config_digest"] = _digest({k: v for k, v in spec.items()
                                             if k not in ("not_configured", "config_digest")})
        else:
            spec["site_unresolved"] = list(result)
    return {name: drive for name, drive in outcome.items() if isinstance(drive, str)}


# ------------------------------------------------------------------ store

_DDL = """
CREATE TABLE IF NOT EXISTS meta (name TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS run (
  run_id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT NOT NULL, finished_at TEXT,
  state TEXT NOT NULL, config_digest TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS run_unit (
  run_id INTEGER NOT NULL, unit_id TEXT NOT NULL, source_id TEXT NOT NULL,
  kind TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT, state TEXT NOT NULL,
  reasons_json TEXT NOT NULL DEFAULT '[]', counts_json TEXT NOT NULL DEFAULT '{}',
  cursor_before_json TEXT, cursor_after_json TEXT, lag_json TEXT NOT NULL DEFAULT '{}',
  PRIMARY KEY (run_id, unit_id));
CREATE TABLE IF NOT EXISTS unit_cursor (
  unit_id TEXT PRIMARY KEY, source_id TEXT NOT NULL, kind TEXT NOT NULL,
  config_digest TEXT NOT NULL, cursor_json TEXT NOT NULL, updated_at TEXT NOT NULL,
  updated_run INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS item (
  unit_id TEXT NOT NULL, item_key TEXT NOT NULL, version TEXT NOT NULL,
  content_sha256 TEXT, citation TEXT, source_time TEXT, record_json TEXT NOT NULL,
  first_seen_run INTEGER NOT NULL, last_seen_run INTEGER NOT NULL,
  last_changed_run INTEGER NOT NULL, last_seen_pass INTEGER NOT NULL DEFAULT 0,
  absent_since_run INTEGER,
  PRIMARY KEY (unit_id, item_key));
"""


class IngestStore:
    """The isolated SQLite store: runs, per-unit results, cursors and item index."""

    def __init__(self, state_dir: str | Path) -> None:
        self.root = Path(state_dir) / STORE_DIR
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.root / STORE_FILE
        self.db = sqlite3.connect(str(self.path), isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA busy_timeout = 10000")
        self.db.execute("PRAGMA journal_mode = WAL")
        self.db.executescript(_DDL)
        self.db.execute("INSERT OR IGNORE INTO meta VALUES ('schema', 'plx-ingest-store/v1')")
        os.chmod(self.path, 0o600)

    def close(self) -> None:
        self.db.close()

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield self.db
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        self.db.execute("COMMIT")

    def cursor(self, unit_id: str) -> dict[str, Any] | None:
        row = self.db.execute("SELECT cursor_json FROM unit_cursor WHERE unit_id = ?",
                              (unit_id,)).fetchone()
        return json.loads(row["cursor_json"]) if row else None

    def set_cursor(self, unit: Mapping[str, Any], cursor: Mapping[str, Any], run_id: int,
                   now: str) -> None:
        self.db.execute(
            """INSERT INTO unit_cursor VALUES (?,?,?,?,?,?,?) ON CONFLICT(unit_id) DO UPDATE
               SET config_digest = excluded.config_digest, cursor_json = excluded.cursor_json,
               updated_at = excluded.updated_at, updated_run = excluded.updated_run""",
            (unit["unit_id"], unit["source_id"], unit["kind"], unit["config_digest"],
             _canonical(cursor), now, run_id))

    def upsert(self, unit_id: str, key: str, version: str, record: Mapping[str, Any], *,
               run_id: int, pass_no: int = 0, source_time: str | None = None) -> str:
        """``new`` / ``changed`` / ``unchanged`` for one item (call inside ``write``)."""
        row = self.db.execute("SELECT version, absent_since_run, record_json FROM item "
                              "WHERE unit_id = ? AND item_key = ?", (unit_id, key)).fetchone()
        citation = record.get("citation") if isinstance(record.get("citation"), str) else None
        content = record.get("content_sha256")
        if row is None:
            self.db.execute("INSERT INTO item VALUES (?,?,?,?,?,?,?,?,?,?,?,NULL)",
                            (unit_id, key, version, content, citation, source_time,
                             _canonical(record), run_id, run_id, run_id, pass_no))
            return "new"
        if row["version"] != version or row["absent_since_run"] is not None:
            self.db.execute(
                """UPDATE item SET version = ?, content_sha256 = ?, citation = ?,
                   source_time = ?, record_json = ?, last_seen_run = ?, last_changed_run = ?,
                   last_seen_pass = ?, absent_since_run = NULL
                   WHERE unit_id = ? AND item_key = ?""",
                (version, content, citation, source_time, _canonical(record), run_id, run_id,
                 pass_no, unit_id, key))
            return "changed"
        # Same source version, but the record can still differ (a SharePoint permission
        # change keeps the eTag while ``source_acl`` changes): keep the newest record so
        # a reader's visibility is never decided from a stale ACL. Rewritten only when
        # it differs.
        if row["record_json"] == _canonical(record):
            self.db.execute("UPDATE item SET last_seen_run = ?, last_seen_pass = ? "
                            "WHERE unit_id = ? AND item_key = ?",
                            (run_id, pass_no, unit_id, key))
            return "unchanged"
        self.db.execute(
            """UPDATE item SET last_seen_run = ?, last_seen_pass = ?, record_json = ?,
               content_sha256 = ?, citation = ? WHERE unit_id = ? AND item_key = ?""",
            (run_id, pass_no, _canonical(record), content, citation, unit_id, key))
        return "unchanged"

    def current_items(self, unit_id: str) -> list[dict[str, Any]]:
        """Stored records of one unit not marked absent (as read for the ingest principal;
        never servable to an employee without a per-reader check)."""
        return [json.loads(row["record_json"]) for row in self.db.execute(
            "SELECT record_json FROM item WHERE unit_id = ? AND absent_since_run IS NULL "
            "ORDER BY item_key", (unit_id,))]

    def current_listing(self, unit_id: str) -> dict[str, dict[str, Any]]:
        """``item key -> stored record`` of a SharePoint folder unit, only for items the
        newest traversal saw: returned by the last finished traversal or by the one in
        progress, and not marked absent. An item withheld (audience or source ACL),
        unlisted or failed since then keeps its older row but is left out here, so a
        caller deciding visibility from the newest ACL fails closed for it."""
        cursor = self.cursor(unit_id) or {}
        started = int(cursor.get("traversals_started") or 0)
        finished = started - 1 if cursor.get("traversal") else started
        if started <= 0:
            return {}
        return {row["item_key"]: json.loads(row["record_json"]) for row in self.db.execute(
            "SELECT item_key, record_json FROM item WHERE unit_id = ? AND "
            "absent_since_run IS NULL AND last_seen_pass >= ?", (unit_id, max(finished, 1)))}

    def mark_absent(self, unit_id: str, pass_no: int, run_id: int) -> int:
        """Items not seen in a clean full pass: absent from it (never a deletion proof)."""
        return self.db.execute(
            "UPDATE item SET absent_since_run = ? WHERE unit_id = ? AND last_seen_pass < ? "
            "AND absent_since_run IS NULL", (run_id, unit_id, pass_no)).rowcount


# ------------------------------------------------------------------ runner

def _within(path: str, root: str) -> bool:
    """``path`` is ``root`` or below it, by whole segments.

    SharePoint paths are case-insensitive, so the comparison lower-cases
    (``str.lower``, never ``casefold``: ``Maß`` must not equal ``Mass`` and a
    ligature must not equal its letters); ``/AOs/2026 - AOs x`` is not under
    ``/AOs/2026 - AOs``. A path or root with a ``.`` or ``..`` segment (or
    any non-string) is never within: a dot segment could climb out of the
    root after the prefix test.
    """
    if not isinstance(path, str) or not isinstance(root, str):
        return False
    if any(segment in (".", "..") for value in (path, root) for segment in value.split("/")):
        return False
    lowered, base = path.lower().rstrip("/"), root.lower().rstrip("/")
    return base == "" or lowered == base or lowered.startswith(base + "/")


def _counts() -> dict[str, int]:
    return {"pages": 0, "items_read": 0, "items_returned": 0, "new": 0, "changed": 0,
            "unchanged": 0, "withheld": 0, "provenance_dropped": 0, "absent_marked": 0}


def _listing_reasons(completeness: Mapping[str, Any]) -> list[str]:
    reasons = []
    if completeness.get("truncated") or not completeness.get("complete", False):
        reasons.append(f"cap:{completeness.get('truncation_reason') or 'truncated'}")
    if completeness.get("provenance_dropped"):
        reasons.append("provenance_dropped")
    if completeness.get("acl_reads_capped"):
        reasons.append("cap:acl_reads")
    withheld = int(completeness.get("withheld_count") or 0) - int(
        completeness.get("provenance_dropped") or 0)
    if withheld > 0:
        # Hidden from the ingest principal: inaccessible, never evidence of absence,
        # so a pass with withheld items is unclean and marks nothing absent.
        reasons.append(f"withheld:{withheld}")
    return reasons


def _add_listing(counts: dict[str, int], completeness: Mapping[str, Any]) -> None:
    counts["pages"] += int(completeness.get("pages_read") or 0)
    counts["items_read"] += int(completeness.get("items_read") or 0)
    counts["items_returned"] += int(completeness.get("items_returned") or 0)
    counts["withheld"] += int(completeness.get("withheld_count") or 0) - int(
        completeness.get("provenance_dropped") or 0)
    counts["provenance_dropped"] += int(completeness.get("provenance_dropped") or 0)


# Counts of what was read (kept when a later step of the unit fails).
_READ_COUNTS = ("pages", "items_read", "items_returned", "withheld", "provenance_dropped")


def _add_partial(counts: dict[str, int], exc: BaseException) -> None:
    """Pages and items a paged read got through before it raised
    (``SourceApi.get_pages`` attaches them); the error reason still records the
    failure, and no item of the failed read is stored."""
    for attr, key in (("pages_read", "pages"), ("items_read", "items_read")):
        value = getattr(exc, attr, None)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            counts[key] += value


class Ingestor:
    """One scheduler-free ingestion pass over configured sources.

    ``mirror`` is the case runtime's ``SourceMirror`` (Portal relations);
    ``mail`` a ``GraphMailReader``; ``sharepoint`` maps drive id ->
    ``SharePointReader``; ``bc`` a ``BusinessCentralReader``; ``mc`` an
    ``integrations.mc_sources.McCaptureReader``. A reader left ``None`` makes its
    sources ``not_configured``. ``actor`` is the ingest principal every read is
    entitlement-checked for.
    """

    def __init__(self, state_dir: str | Path, sources: Sequence[Mapping[str, Any]], *,
                 actor: Actor, mirror: Any = None, mail: Any = None,
                 sharepoint: Mapping[str, Any] | None = None, bc: Any = None,
                 mc: Any = None,
                 clock: Callable[[], datetime] | None = None) -> None:
        if not isinstance(actor, Actor):
            raise IngestError("ingest_actor_invalid")
        self.state_dir = Path(state_dir)
        self.sources = [dict(s) for s in sources]
        self.actor = actor
        self.mirror, self.mail, self.bc, self.mc = mirror, mail, bc, mc
        self.sharepoint = dict(sharepoint or {})
        self._clock = clock or (lambda: datetime.now(_UTC))
        # Ingestion writes real captures: label the directory live (a fixture label
        # or seed marker refuses), so a later fixture host cannot reuse it.
        from .session_identity import SessionIdentityRefused, bind_state_dir

        try:
            bind_state_dir(self.state_dir, fixture=False)
        except SessionIdentityRefused as error:
            raise IngestError("ingest_state_is_fixture") from error
        self.store = IngestStore(self.state_dir)
        self._portal_before: dict[str, Any] | None = None

    def close(self) -> None:
        self.store.close()

    def _now(self) -> datetime:
        moment = self._clock()
        if not isinstance(moment, datetime) or moment.tzinfo is None:
            raise IngestError("ingest_clock_invalid")
        return moment.astimezone(_UTC)

    @contextmanager
    def _lock(self) -> Iterator[None]:
        descriptor = os.open(self.store.root / "ingest.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise IngestError("ingest_run_in_progress") from None
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    # -- run ----------------------------------------------------------------

    def run(self, *, only: Sequence[str] | None = None) -> dict[str, Any]:
        """Advance every (or ``only`` the named) source once; counts-only summary."""
        selected = [s for s in self.sources if only is None or s["id"] in set(only)]
        with self._lock():
            db = self.store.db
            now = _now_iso(self._now())
            with self.store.write():
                # Holding the lock, any run still 'running' died without finishing.
                interrupted = db.execute("UPDATE run SET state = 'interrupted' "
                                         "WHERE state = 'running'").rowcount
                db.execute("UPDATE run_unit SET state = 'interrupted' WHERE state = 'running'")
                run_id = db.execute(
                    "INSERT INTO run (started_at, state, config_digest) VALUES (?,?,?)",
                    (now, "running",
                     _digest(sorted(s["config_digest"] for s in selected)))).lastrowid
            self._portal_before = None
            units = []
            for spec in selected:
                units.extend(self._run_source(run_id, spec))
            with self.store.write():
                db.execute("UPDATE run SET state = 'finished', finished_at = ? WHERE run_id = ?",
                           (_now_iso(self._now()), run_id))
        return {"schema": RUN_SCHEMA, "run_id": run_id,
                "previous_runs_interrupted": interrupted,
                "units": units, "summary": _summary(units)}

    def _begin(self, run_id: int, unit: Mapping[str, Any]) -> dict[str, Any] | None:
        """Record the unit as running; return the cursor it may continue from.

        A unit whose configured scope changed (config digest) never inherits
        the old high-water mark or traversal frontier; only its pass counters
        carry over, so items of the old scope that a clean pass of the new
        scope does not return are marked absent."""
        before = self.store.cursor(unit["unit_id"])
        stored = self.store.db.execute("SELECT config_digest FROM unit_cursor WHERE unit_id = ?",
                                       (unit["unit_id"],)).fetchone()
        with self.store.write():
            self.store.db.execute(
                """INSERT OR REPLACE INTO run_unit (run_id, unit_id, source_id, kind, started_at,
                   state, cursor_before_json) VALUES (?,?,?,?,?,?,?)""",
                (run_id, unit["unit_id"], unit["source_id"], unit["kind"],
                 _now_iso(self._now()), "running", _canonical(before)))
        if stored is not None and stored["config_digest"] != unit["config_digest"]:
            return {key: before[key] for key in ("traversals_started", "scans")
                    if isinstance(before, dict) and key in before}
        return before

    def _finish(self, run_id: int, unit: Mapping[str, Any], reasons: Sequence[str],
                counts: Mapping[str, int], lag: Mapping[str, Any],
                cursor_after: Any = None, *, use_stored_cursor: bool = True,
                state: str | None = None) -> dict[str, Any]:
        reasons = sorted(set(reasons))
        for reason in reasons:
            if _REASON.fullmatch(reason) is None:
                raise AssertionError("reason shape")  # pragma: no cover - programming error
        if use_stored_cursor:
            cursor_after = self.store.cursor(unit["unit_id"])
        state = state or ("complete" if not reasons else "incomplete")
        self.store.db.execute(
            """UPDATE run_unit SET finished_at = ?, state = ?, reasons_json = ?, counts_json = ?,
               cursor_after_json = ?, lag_json = ? WHERE run_id = ? AND unit_id = ?""",
            (_now_iso(self._now()), state, _canonical(reasons), _canonical(dict(counts)),
             _canonical(cursor_after), _canonical(dict(lag)), run_id, unit["unit_id"]))
        return {"unit_id": unit["unit_id"], "source_id": unit["source_id"],
                "kind": unit["kind"], "state": state, "reasons": reasons,
                "counts": dict(counts), "lag": _lag_numbers(lag)}

    def _unit(self, spec: Mapping[str, Any], suffix: str | None = None,
              **extra: Any) -> dict[str, Any]:
        unit_id = spec["id"] if suffix is None else f"{spec['id']}.{suffix}"
        return {"unit_id": unit_id, "source_id": spec["id"], "kind": spec["kind"],
                "config_digest": spec["config_digest"], "spec": spec, **extra}

    def _not_ready(self, run_id: int, unit: Mapping[str, Any], reasons: list[str]
                   ) -> dict[str, Any]:
        self._begin(run_id, unit)
        with self.store.write():
            return self._finish(run_id, unit, reasons, _counts(), {})

    def _run_source(self, run_id: int, spec: Mapping[str, Any]) -> list[dict[str, Any]]:
        if spec["not_configured"]:
            return [self._not_ready(run_id, self._unit(spec),
                                    [f"not_configured:{n}" for n in spec["not_configured"]])]
        if spec.get("site_unresolved"):
            return [self._not_ready(run_id, self._unit(spec), list(spec["site_unresolved"]))]
        kind = spec["kind"]
        reader = {"portal_relation": self.mirror, "mail_folder": self.mail,
                  "bc_entity_set": self.bc, "mc_sources": self.mc,
                  "sharepoint_folder": self.sharepoint.get(spec.get("drive_id"))}[kind]
        if reader is None:
            return [self._not_ready(run_id, self._unit(spec), ["not_configured:reader"])]
        if kind == "bc_entity_set":
            return self._run_bc(run_id, spec)
        if kind == "mc_sources":
            return self._run_mc(run_id, spec)
        unit = self._unit(spec)
        before = self._begin(run_id, unit)
        read = _counts()  # filled by the mail/SharePoint runs as their reads finish
        try:
            if kind == "portal_relation":
                return [self._run_portal(run_id, unit)]
            if kind == "mail_folder":
                return [self._run_mail(run_id, unit, before, read)]
            return [self._run_sharepoint(run_id, unit, before, read)]
        except Exception as exc:  # noqa: BLE001 - every source failure is recorded by class
            # Reads that finished before the failure keep their counts, plus the
            # pages the failing read got through; store outcomes (new, changed,
            # unchanged, absent_marked) are not kept: that write did not commit.
            counts = _counts()
            for key in _READ_COUNTS:
                counts[key] = int(read.get(key) or 0)
            _add_partial(counts, exc)
            with self.store.write():
                return [self._finish(run_id, unit, [_error_reason(exc)], counts, {})]

    # -- Portal (mirror) ------------------------------------------------------

    def _run_portal(self, run_id: int, unit: Mapping[str, Any]) -> dict[str, Any]:
        relation = unit["spec"]["relation"]
        if self._portal_before is None:
            catalog = self.mirror.catalog(self.actor)
            self._portal_before = {r["name"]: r.get("freshness") for r in catalog["relations"]}
        before = self._portal_before.get(relation) or {}
        cursor_before = {"watermark": before.get("source_watermark")}
        fresh = self.mirror.refresh(self.actor, relation)
        reasons = []
        if fresh.get("refresh_error"):
            reasons.append(_mirror_reason(str(fresh["refresh_error"])))
        cursor_after = {"watermark": fresh.get("source_watermark")}
        counts = _counts()
        if not reasons:
            # The mirror reports rows per refresh, not pages.
            counts["items_read"] = int(fresh.get("refresh_rows_received") or 0)
        counts["rows_current"] = int(fresh.get("row_count") or 0)
        counts["rows_absent_from_latest_full_traversal"] = int(
            fresh.get("rows_absent_from_latest_full_traversal") or 0)
        health = fresh.get("health_after") or {}
        lag: dict[str, Any] = {"replica_lag_seconds": health.get("replay_lag_seconds"),
                               "watermark_age_seconds": fresh.get("source_watermark_age_seconds")}
        replica, watermark = _instant(health.get("generated_at")), _instant(
            fresh.get("source_watermark"))
        if replica and watermark:
            lag["watermark_behind_replica_seconds"] = max(
                0.0, round((replica - watermark).total_seconds(), 3))
        counts["refresh_mode_incremental"] = int(fresh.get("refreshed_now") == "incremental")
        with self.store.write():
            self.store.set_cursor(unit, {"portal": cursor_after,
                                         "mode": fresh.get("refreshed_now")},
                                  run_id, _now_iso(self._now()))
            result = self._finish(run_id, unit, reasons, counts, lag,
                                  {"portal": cursor_after}, use_stored_cursor=False)
            self.store.db.execute("UPDATE run_unit SET cursor_before_json = ? WHERE run_id = ? "
                                  "AND unit_id = ?", (_canonical({"portal": cursor_before}),
                                                      run_id, unit["unit_id"]))
        return result

    # -- mail ----------------------------------------------------------------

    def _run_mail(self, run_id: int, unit: Mapping[str, Any],
                  before: Mapping[str, Any] | None,
                  counts: dict[str, int]) -> dict[str, Any]:
        spec = unit["spec"]
        now = self._now()
        high_water = _instant((before or {}).get("received_high_water"))
        # Composite cursor (received second, message ids read at that second).
        known_ids = set((before or {}).get("ids_at_high_water") or [])
        recovering = bool((before or {}).get("stalled"))
        page_size, max_pages, max_items = spec["page_size"], spec["max_pages"], spec["max_items"]
        if high_water is None:
            if spec["start_at"]:
                start = _instant(spec["start_at"])
            elif spec.get("initial_window_anchor") == "newest_message":
                # One newest-first metadata GET (never stored or counted as read):
                # the window ends at the folder's newest message, never later than
                # now. An empty folder anchors at now.
                newest = self.mail.list_messages(self.actor, spec["mailbox"],
                                                 folder=spec["folder"], page_size=1,
                                                 max_pages=1, max_items=1)
                top = (_instant(newest["items"][0].get("received_at"))
                       if newest["items"] else None)
                counts["initial_window_anchored"] = int(top is not None)
                anchor = min(top, now) if top is not None else now
                start = anchor - timedelta(days=spec["initial_window_days"])
            else:
                start = now - timedelta(days=spec["initial_window_days"])
            since = start
        elif recovering:
            # After a stall: one widened read that still covers the overlap
            # window (a late message received inside it is read), tie-broken on
            # the ids already read at the high-water second.
            start = None
            since = high_water - timedelta(seconds=spec["overlap_seconds"])
            page_size, max_pages, max_items = (50, -(-STALL_RECOVERY_ITEMS // 50),
                                               STALL_RECOVERY_ITEMS)
        else:
            start = None
            since = high_water - timedelta(seconds=spec["overlap_seconds"])

        def read(moment: datetime, *, widened: bool = False) -> dict[str, Any]:
            size, pages, items = ((50, -(-STALL_RECOVERY_ITEMS // 50), STALL_RECOVERY_ITEMS)
                                  if widened else (page_size, max_pages, max_items))
            return self.mail.list_messages(
                self.actor, spec["mailbox"], folder=spec["folder"],
                received_since=_graph_since(moment), page_size=size,
                max_pages=pages, max_items=items, oldest_first=True)

        def newest_of(items: Sequence[Mapping[str, Any]]) -> datetime | None:
            moments = [m for m in (_instant(r.get("received_at")) for r in items)
                       if m is not None]
            return max(moments) if moments else None

        counts["stall_recovery_read"] = int(recovering)
        listing = read(since)
        gap: dict[str, str] | None = None
        if (recovering and since != high_water and listing["completeness"].get("truncated")
                and not any(_instant(r.get("received_at")) is not None
                            and _instant(r.get("received_at")) > high_water
                            for r in listing["items"])):
            # Even the widened read ended inside the overlap window: read from the
            # high-water second so the cursor progresses. The unread rest of the
            # window [newest read, high water] is persisted as a gap; later runs
            # read it before the unit can report complete.
            overlap_items = listing["items"]
            _add_listing(counts, listing["completeness"])  # this read happened too
            gap = {"from": _graph_since(newest_of(overlap_items) or since),
                   "to": _graph_since(high_water)}
            listing = read(high_water)
            seen = {str(r["id"]) for r in listing["items"]}
            listing = {**listing, "items": [r for r in overlap_items if str(r["id"]) not in seen]
                       + listing["items"]}
        completeness = listing["completeness"]
        _add_listing(counts, completeness)
        reasons = _listing_reasons(completeness)
        gap_items: list[Mapping[str, Any]] = []
        pending = (before or {}).get("overlap_gap")
        gap_from = _instant(pending.get("from")) if isinstance(pending, Mapping) else None
        gap_to = _instant(pending.get("to")) if isinstance(pending, Mapping) else None
        if gap_from is not None and gap_to is not None:
            # A window an earlier run could not read: one widened read from its
            # start. It is closed only when the read ends (not truncated) or
            # reaches its end second (that second belongs to the main cursor's
            # high-water tie-break, read since by the high-water read).
            counts["overlap_gap_read"] = 1
            try:
                window = read(gap_from, widened=True)
            except Exception as exc:  # noqa: BLE001 - recorded; the gap stays open
                _add_partial(counts, exc)
                reasons.append(_error_reason(exc))
                remaining: dict[str, str] | None = {"from": _graph_since(gap_from),
                                                    "to": _graph_since(gap_to)}
            else:
                _add_listing(counts, window["completeness"])
                gap_items = [r for r in window["items"]
                             if (_instant(r.get("received_at")) or gap_to) <= gap_to]
                newest_gap = newest_of(window["items"])
                closed = (not window["completeness"].get("truncated")
                          or (newest_gap is not None and newest_gap >= gap_to))
                remaining = None if closed else {
                    "from": _graph_since(max(gap_from, newest_of(gap_items) or gap_from)),
                    "to": _graph_since(gap_to)}
            if remaining is not None and gap is not None:
                gap = {"from": min(gap["from"], remaining["from"]),
                       "to": max(gap["to"], remaining["to"])}
            elif remaining is not None:
                gap = remaining
        if gap is not None:
            reasons.append("overlap_window_unread")
        newest_read = None
        for record in listing["items"]:
            received = _instant(record.get("received_at"))
            if received is not None and (newest_read is None or received > newest_read):
                newest_read = received
        candidates = [m for m in (high_water, newest_read, start) if m is not None]
        new_high_water = max(candidates) if candidates else None
        at_high_water = {str(r["id"]) for r in listing["items"]
                         if new_high_water is not None
                         and _instant(r.get("received_at")) == new_high_water}
        if new_high_water == high_water:
            at_high_water |= known_ids
        stalled = bool(completeness.get("truncated") and high_water is not None
                       and (newest_read is None or newest_read <= high_water)
                       and at_high_water <= known_ids)
        if stalled:
            reasons.append("cursor_stalled")
        # Source high-water: the newest message the folder holds (one metadata GET).
        lag: dict[str, Any] = {}
        try:
            newest = self.mail.list_messages(self.actor, spec["mailbox"], folder=spec["folder"],
                                             page_size=1, max_pages=1, max_items=1)
            top = _instant(newest["items"][0].get("received_at")) if newest["items"] else None
            lag["source_high_water_known"] = top is not None or not newest["completeness"].get(
                "items_read")
            if top is not None and new_high_water is not None:
                lag["lag_seconds"] = max(0.0, round((top - new_high_water).total_seconds(), 3))
            elif lag["source_high_water_known"]:
                lag["lag_seconds"] = 0.0
        except Exception as exc:  # noqa: BLE001 - lag is advisory; its failure is recorded
            lag["source_high_water_known"] = False
            lag["high_water_error"] = _error_reason(exc)
        cursor = {"received_high_water": _graph_since(new_high_water) if new_high_water
                  else None,
                  "ids_at_high_water": sorted(at_high_water)[:STALL_RECOVERY_ITEMS],
                  "stalled": stalled, "overlap_gap": gap}
        listed = {str(r["id"]) for r in listing["items"]}
        with self.store.write():
            for record in list(listing["items"]) + [r for r in gap_items
                                                    if str(r["id"]) not in listed]:
                outcome = self.store.upsert(
                    unit["unit_id"], str(record["id"]), str(record["change_key"]), record,
                    run_id=run_id, source_time=record.get("received_at"))
                counts[outcome] += 1
            self.store.set_cursor(unit, cursor, run_id, _now_iso(self._now()))
            return self._finish(run_id, unit, reasons, counts, lag)

    # -- SharePoint ----------------------------------------------------------

    def _run_sharepoint(self, run_id: int, unit: Mapping[str, Any],
                        before: Mapping[str, Any] | None,
                        counts: dict[str, int]) -> dict[str, Any]:
        spec = unit["spec"]
        reader = self.sharepoint[spec["drive_id"]]
        cursor = dict(before or {})
        traversal = cursor.get("traversal")
        if traversal is None:
            number = int(cursor.get("traversals_started") or 0) + 1
            traversal = {"number": number, "pending": [[spec["path"], 0]], "folders_listed": 0,
                         "unclean": [], "started_at": _now_iso(self._now())}
            cursor["traversals_started"] = number
        counts["folders_listed"] = 0
        reasons: list[str] = []
        failed = False
        from integrations.live_source_guard import NotVisible, SourceNotFound

        def drop_head(reason: str) -> None:
            # A queued folder that can no longer be listed is dropped so the walk
            # can finish; the traversal ends unclean (nothing is marked absent).
            traversal["pending"] = traversal["pending"][1:]
            traversal.pop("head_failures", None)
            traversal["unclean"] = sorted(set(traversal["unclean"]) | {reason})
            reasons.append(reason)
            counts["folders_dropped"] = counts.get("folders_dropped", 0) + 1
            with self.store.write():
                cursor["traversal"] = traversal
                self.store.set_cursor(unit, cursor, run_id, _now_iso(self._now()))

        while traversal["pending"] and counts["folders_listed"] < spec["max_folders_per_run"]:
            entry = traversal["pending"][0]
            path, depth = entry[0], entry[1]
            item_id = entry[2] if len(entry) > 2 else None
            try:
                listing = reader.list_folder(self.actor, path, page_size=spec["page_size"],
                                             max_pages=spec["max_pages"],
                                             max_items=spec["max_items"])
            except (NotVisible, SourceNotFound) as exc:
                _add_partial(counts, exc)  # pages read before the refusal still count
                if depth == 0:  # the configured folder itself: retried every run
                    reasons.append(_error_reason(exc))
                    failed = True
                    break
                # Renamed or moved: the folder's item id still resolves to its new path.
                moved = None
                if item_id is not None:
                    try:
                        found = reader.get_item(self.actor, item_id)
                    except Exception:  # noqa: BLE001 - the drop below records it
                        found = None
                    if (found is not None and found.get("kind") == "folder"
                            and found.get("path") != path):
                        moved = found["path"]
                if moved is not None and _within(moved, spec["path"]):
                    traversal["pending"][0] = [moved, depth, item_id]
                    counts["folders_relocated"] = counts.get("folders_relocated", 0) + 1
                    continue
                if moved is not None:
                    # Moved outside the configured folder: not this source's scope.
                    drop_head("folder_moved_out")
                    continue
                # Deleted, moved where the ingest principal cannot see it, or hidden
                # by an audience/ACL change: the reader reads all of these as not
                # visible (on purpose), so the label does not claim "gone".
                drop_head("folder_not_visible")
                continue
            except Exception as exc:  # noqa: BLE001 - recorded; the folder stays pending
                # The pages this listing read before it failed are counted (the
                # folder's items are not stored; the folder is listed again).
                _add_partial(counts, exc)
                failures = int(traversal.get("head_failures") or 0) + 1
                if depth > 0 and failures >= FOLDER_FAILURE_LIMIT:
                    drop_head(f"folder_failed:{type(exc).__name__}")
                    continue
                traversal["head_failures"] = failures
                reasons.append(_error_reason(exc))
                failed = True
                with self.store.write():
                    cursor["traversal"] = traversal
                    self.store.set_cursor(unit, cursor, run_id, _now_iso(self._now()))
                break
            traversal.pop("head_failures", None)
            completeness = listing["completeness"]
            _add_listing(counts, completeness)
            folder_reasons = _listing_reasons(completeness)
            reasons.extend(folder_reasons)
            traversal["unclean"] = sorted(set(traversal["unclean"]) | set(folder_reasons))
            traversal["pending"] = traversal["pending"][1:]
            for record in listing["items"]:
                if (record.get("kind") == "folder" and spec["recursive"]
                        and depth < spec["max_depth"]):
                    traversal["pending"].append([record["path"], depth + 1, record["id"]])
            if (spec["recursive"] and depth >= spec["max_depth"]
                    and any(r.get("kind") == "folder" for r in listing["items"])):
                traversal["unclean"] = sorted(set(traversal["unclean"]) | {"cap:max_depth"})
                reasons.append("cap:max_depth")
            traversal["folders_listed"] += 1
            counts["folders_listed"] += 1
            with self.store.write():
                for record in listing["items"]:
                    outcome = self.store.upsert(
                        unit["unit_id"], str(record["id"]), str(record["etag"]), record,
                        run_id=run_id, pass_no=traversal["number"],
                        source_time=record.get("last_modified"))
                    counts[outcome] += 1
                cursor["traversal"] = traversal
                self.store.set_cursor(unit, cursor, run_id, _now_iso(self._now()))
        lag: dict[str, Any] = {}
        with self.store.write():
            if not traversal["pending"] and not failed:
                if traversal["unclean"]:
                    reasons.extend(traversal["unclean"])
                else:
                    counts["absent_marked"] = self.store.mark_absent(
                        unit["unit_id"], traversal["number"], run_id)
                    cursor["last_complete_traversal"] = traversal["number"]
                    cursor["last_complete_traversal_at"] = _now_iso(self._now())
                cursor["traversal"] = None
                self.store.set_cursor(unit, cursor, run_id, _now_iso(self._now()))
            elif traversal["pending"]:
                reasons.append("traversal_in_progress")
            done_at = _instant(cursor.get("last_complete_traversal_at"))
            if done_at is not None:
                lag["last_complete_traversal_age_seconds"] = round(
                    (self._now() - done_at).total_seconds(), 3)
            lag["folders_pending"] = len(traversal["pending"]) if cursor.get("traversal") else 0
            return self._finish(run_id, unit, reasons, counts, lag)

    # -- Business Central ----------------------------------------------------

    def _run_bc(self, run_id: int, spec: Mapping[str, Any]) -> list[dict[str, Any]]:
        results = []
        companies = spec["companies"]
        if companies == "all":
            listing_unit = self._unit(spec, "companies")
            self._begin(run_id, listing_unit)
            try:
                listing = self.bc.companies(self.actor)
            except Exception as exc:  # noqa: BLE001
                counts = _counts()
                _add_partial(counts, exc)
                with self.store.write():
                    return [self._finish(run_id, listing_unit, [_error_reason(exc)],
                                         counts, {})]
            counts = _counts()
            _add_listing(counts, listing["completeness"])
            companies = sorted({str(c.get("id")).lower() for c in listing["items"]
                                if isinstance(c.get("id"), str) and _GUID.fullmatch(c["id"])})
            counts["companies"] = len(companies)
            with self.store.write():
                results.append(self._finish(run_id, listing_unit,
                                            _listing_reasons(listing["completeness"]),
                                            counts, {}, None, use_stored_cursor=False))
        for company in companies:
            unit = self._unit(spec, sha256(company.encode()).hexdigest()[:10], company=company)
            before = self._begin(run_id, unit)
            try:
                results.append(self._run_bc_unit(run_id, unit, before))
            except Exception as exc:  # noqa: BLE001
                # A scan that failed on a later page keeps the pages it read
                # (nothing of it is stored; the cursor stays where it was).
                counts = _counts()
                _add_partial(counts, exc)
                with self.store.write():
                    results.append(self._finish(run_id, unit, [_error_reason(exc)],
                                                counts, {}))
        return results

    def _run_bc_unit(self, run_id: int, unit: Mapping[str, Any],
                     before: Mapping[str, Any] | None) -> dict[str, Any]:
        spec = unit["spec"]
        cursor = dict(before or {})
        number = int(cursor.get("scans") or 0) + 1
        listing = self.bc.list_entities(self.actor, unit["company"], spec["entity_set"],
                                        page_size=spec["page_size"],
                                        max_pages=spec["max_pages"],
                                        max_items=spec["max_items"])
        completeness = listing["completeness"]
        counts = _counts()
        _add_listing(counts, completeness)
        reasons = _listing_reasons(completeness)
        newest = _instant(cursor.get("high_water_modified"))
        for record in listing["items"]:
            moment = _instant(record.get("last_modified"))
            if moment is not None and (newest is None or moment > newest):
                newest = moment
        lag: dict[str, Any] = {}
        with self.store.write():
            for record in listing["items"]:
                outcome = self.store.upsert(unit["unit_id"], str(record["id"]).lower(),
                                            str(record["etag"]), record, run_id=run_id,
                                            pass_no=number,
                                            source_time=record.get("last_modified"))
                counts[outcome] += 1
            cursor["scans"] = number
            cursor["high_water_modified"] = _now_iso(newest) if newest else None
            if not reasons:
                counts["absent_marked"] = self.store.mark_absent(unit["unit_id"], number,
                                                                 run_id)
                cursor["last_complete_scan"] = number
                cursor["last_complete_scan_at"] = _now_iso(self._now())
                lag["lag_seconds"] = 0.0  # a clean full scan saw the whole set
            self.store.set_cursor(unit, cursor, run_id, _now_iso(self._now()))
            return self._finish(run_id, unit, reasons, counts, lag)


    # -- Mission Control (capture store) ------------------------------------------

    def _run_mc(self, run_id: int, spec: Mapping[str, Any]) -> list[dict[str, Any]]:
        """One MC capture cycle; one unit per collection, its capture state as reasons
        (``in_progress`` -> ``traversal_in_progress`` plus the pause reason)."""
        from integrations.mc_sources import CaptureSpec

        options = {k: spec[k] for k in ("scope", "knowledge_queries", "knowledge_limit",
                                        "max_task_reads", "page_size") if k in spec}
        options["knowledge_queries"] = tuple(options.get("knowledge_queries", ()))
        try:
            captured = self.mc.capture(CaptureSpec(**options))
        except Exception as exc:  # noqa: BLE001 - recorded by class, like every source
            unit = self._unit(spec)
            self._begin(run_id, unit)
            with self.store.write():
                return [self._finish(run_id, unit, [_error_reason(exc)], _counts(), {})]
        results = []
        for item in captured["units"]:
            unit = self._unit(spec, item["collection"])
            self._begin(run_id, unit)
            reasons = []
            if item["state"] == "in_progress":
                reasons.append("traversal_in_progress")
            if item["state"] != "complete":
                reason = item.get("reason") or item["state"]
                if reason == "source_error" and item.get("error"):
                    reason = f"source_error:{item['error']}"
                reasons.append(reason if _REASON.fullmatch(reason) else "source_error")
            withheld = item.get("withheld_project_grant")
            if withheld is None:
                reasons.append("withheld:project_grant:not_measured")
            elif withheld:
                reasons.append(f"withheld:project_grant:{int(withheld)}")
            counts = _counts()
            counts.update(pages=item["pages"], items_read=item["records"],
                          items_returned=item["records"], new=item["new_versions"],
                          unchanged=max(0, item["records"] - item["new_versions"]),
                          withheld=withheld or 0)
            with self.store.write():
                results.append(self._finish(
                    run_id, unit, reasons, counts, {},
                    {"capture_state": item["state"], "records_current": item["records_current"]},
                    use_stored_cursor=False))
        return results


# ------------------------------------------------------------------ status

def _run_in_progress(store: IngestStore) -> bool:
    """Whether another process holds the run lock right now."""
    descriptor = os.open(store.root / "ingest.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    else:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        return False
    finally:
        os.close(descriptor)


def _lag_numbers(lag: Mapping[str, Any]) -> dict[str, Any]:
    result = {}
    for key, value in lag.items():
        if value is None or isinstance(value, bool) or (
                isinstance(value, (int, float)) and not isinstance(value, bool)):
            result[key] = value
        elif isinstance(value, str) and _REASON.fullmatch(value):
            result[key] = value
    return result


def _summary(units: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    states = [u["state"] for u in units]
    return {"units": len(units), "complete": states.count("complete"),
            "incomplete": states.count("incomplete"),
            "interrupted": states.count("interrupted"),
            "never_run": states.count("never_run")}


def status(state_dir: str | Path, *, sources: Sequence[Mapping[str, Any]] = (),
           clock: Callable[[], datetime] | None = None) -> dict[str, Any]:
    """Counts-only ingestion status for the console, MCP and the live verifier.

    Per unit: last state, its exact reasons, counts, numeric lag, current and
    absent item counts and the age of its last complete run. Never row data:
    no item ids, citations, names, subjects or cursor values."""
    now = (clock or (lambda: datetime.now(_UTC)))()
    store = IngestStore(state_dir)
    try:
        db = store.db
        runs = db.execute("SELECT COUNT(*) AS total, "
                          "COALESCE(SUM(state = 'interrupted'), 0) AS interrupted, "
                          "COALESCE(SUM(state = 'running'), 0) AS running, "
                          "MAX(run_id) AS last FROM run").fetchone()
        last_state = None
        if runs["last"] is not None:
            last_state = db.execute("SELECT state FROM run WHERE run_id = ?",
                                    (runs["last"],)).fetchone()["state"]
        running_now = _run_in_progress(store)
        units: dict[str, dict[str, Any]] = {}
        for row in db.execute("SELECT * FROM run_unit ORDER BY run_id"):
            units[row["unit_id"]] = dict(row)
        complete_at = {row["unit_id"]: row["at"] for row in db.execute(
            "SELECT unit_id, MAX(finished_at) AS at FROM run_unit WHERE state = 'complete' "
            "GROUP BY unit_id")}
        items = {row["unit_id"]: (row["current"], row["absent"]) for row in db.execute(
            "SELECT unit_id, COALESCE(SUM(absent_since_run IS NULL), 0) AS current, "
            "COALESCE(SUM(absent_since_run IS NOT NULL), 0) AS absent FROM item "
            "GROUP BY unit_id")}
        out = []
        for unit_id, row in sorted(units.items()):
            state = row["state"]
            if state == "running" and not running_now:
                state = "interrupted"  # its run died: nobody holds the run lock
            before = json.loads(row["cursor_before_json"]) if row["cursor_before_json"] else None
            after = json.loads(row["cursor_after_json"]) if row["cursor_after_json"] else None
            done = _instant(complete_at.get(unit_id))
            current, absent = items.get(unit_id, (0, 0))
            out.append({
                "unit_id": unit_id, "source_id": row["source_id"], "kind": row["kind"],
                "completeness_basis": COMPLETENESS_BASIS[row["kind"]],
                "state": state, "last_run_id": row["run_id"],
                "reasons": [r for r in json.loads(row["reasons_json"])
                            if _REASON.fullmatch(r)],
                "counts": {k: v for k, v in json.loads(row["counts_json"]).items()
                           if isinstance(v, int) and not isinstance(v, bool)},
                "lag": _lag_numbers(json.loads(row["lag_json"])),
                "cursor_advanced": (after is not None and after != before
                                    and state != "interrupted"),
                "items_current": current, "items_absent": absent,
                "last_complete_age_seconds": (round((now - done).total_seconds(), 3)
                                              if done else None)})
        seen = {u["source_id"] for u in out}
        for spec in sources:
            if spec["id"] not in seen:
                out.append({"unit_id": spec["id"], "source_id": spec["id"],
                            "kind": spec["kind"],
                            "completeness_basis": COMPLETENESS_BASIS[spec["kind"]],
                            "state": "never_run", "last_run_id": None,
                            "reasons": [], "counts": {}, "lag": {}, "cursor_advanced": False,
                            "items_current": 0, "items_absent": 0,
                            "last_complete_age_seconds": None})
        summary = _summary(out)
        summary["items_current"] = sum(u["items_current"] for u in out)
        return {"schema": STATUS_SCHEMA,
                "runs": {"total": runs["total"],
                         "interrupted": runs["interrupted"] + (0 if running_now
                                                               else runs["running"]),
                         "running": runs["running"] if running_now else 0,
                         "last_run_id": runs["last"],
                         "last_run_state": ("interrupted" if last_state == "running"
                                            and not running_now else last_state)},
                "units": out, "summary": summary,
                "completeness_rule": "complete only when a unit has no reason; a cap, "
                                     "truncation, error or unfinished traversal is incomplete",
                "caveats": ["Portal pages are not an atomic snapshot; deletions are never "
                            "proven.", "Mail completeness is by received time only: "
                            "'complete' means every message received at or after the cursor "
                            "minus overlap was read. Messages moved into the folder later "
                            "with an older received time are not ingested, and edits, moves "
                            "and deletions of ingested messages are not tracked; a complete "
                            "mail unit is never proof that a message is absent.",
                            "Withheld items are inaccessible to the ingest principal, not "
                            "absent; absence is marked only after a clean full pass and is "
                            "never a deletion proof.",
                            "BC has no modified-since read in the closed grammar: every run "
                            "is a bounded full scan."]}
    finally:
        store.close()


def row_data_check(state_dir: str | Path, text: str, *, limit: int = 200_000,
                   since_run: int | None = None) -> dict[str, Any]:
    """Self-check that ``text`` (a serialized status/run output) holds no stored row
    data: no item key, version, citation or string field value (>= 8 chars).

    ``since_run`` limits the check to items seen in that run or later (the
    worker's cheap per-cycle check; its CLI output gets the full check)."""
    store = IngestStore(state_dir)
    checked = leaked = 0
    try:
        values: set[str] = set()
        query, params = ("SELECT item_key, version, citation, record_json FROM item "
                         "LIMIT ?", (limit,))
        if since_run is not None:
            query, params = ("SELECT item_key, version, citation, record_json FROM item "
                             "WHERE last_seen_run >= ? LIMIT ?", (since_run, limit))
        for row in store.db.execute(query, params):
            values.update(v for v in (row["item_key"], row["version"], row["citation"]) if v)
            stack = [json.loads(row["record_json"])]
            while stack:
                node = stack.pop()
                if isinstance(node, dict):
                    stack.extend(node.values())
                elif isinstance(node, list):
                    stack.extend(node)
                elif isinstance(node, str):
                    values.add(node)
        for value in values:
            if len(value) < 8:
                continue
            checked += 1
            leaked += value in text
    finally:
        store.close()
    return {"values_checked": checked, "leaked": leaked}


# ------------------------------------------------------------------ CLI

def _build(args: argparse.Namespace, environ: Mapping[str, str], *,
           graph_sender: Any = None) -> tuple[Ingestor, Any]:
    """Ingestor + case runtime from CLI args; ``graph_sender`` is for
    injected-transport tests only (default: the guard's network sender)."""
    tenant, actor, sources = load_sources_file(args.sources, environ)
    kinds = {s["kind"] for s in sources}
    runtime = None
    mirror = None
    if "portal_relation" in kinds:
        from operating.case_runtime import CaseRuntime

        client = None
        base_url, key = environ.get(args.base_url_env), environ.get(args.key_env)
        if base_url and key:
            from integrations.workbench_read_api import WorkbenchReadApiClient
            client = WorkbenchReadApiClient(base_url, key)
        if args.mirror_audience is None:
            raise IngestError("ingest_mirror_audience_required")
        if client is not None:
            runtime = CaseRuntime(args.state_dir, client=client, audience=args.mirror_audience,
                                  tenant=tenant)
            mirror = runtime.mirror
        else:
            for spec in sources:
                if spec["kind"] == "portal_relation" and not spec["not_configured"]:
                    spec["not_configured"] = [args.key_env if base_url else args.base_url_env]
    mail = bc = mc = None
    sharepoint: dict[str, Any] = {}
    if "mc_sources" in kinds:
        mc = _mc_reader(args, environ, tenant, sources)
    if kinds & {"mail_folder", "sharepoint_folder", "bc_entity_set"}:
        if args.live_audience is None:
            raise IngestError("ingest_live_audience_required")
        from operating.case_runtime import build_live_readers

        sites: dict[str, str] = {}
        if any(s["kind"] == "sharepoint_folder" and s.get("site") and not s["not_configured"]
               for s in sources):
            from integrations import graph_read
            from integrations.live_source_guard import SourceUnavailable

            try:
                api, _transport = graph_read.build_graph(environ, sender=graph_sender)
            except (SourceUnavailable, ValueError):
                api = None  # no Graph credentials: the site's unit reports not_configured
            if api is not None:
                sites = resolve_sites(sources, api, environ,
                                      granted=live_audience_sites(args.live_audience))
        audience = load_live_audience(args.live_audience, environ, sites=sites)
        if audience.tenant != tenant:
            raise IngestError("ingest_live_audience_tenant_mismatch")
        drives = sorted({s["drive_id"] for s in sources
                         if s["kind"] == "sharepoint_folder" and s.get("drive_id")})
        readers = build_live_readers(audience, environ, sharepoint_drives=drives,
                                     graph_sender=graph_sender)
        mail, bc, sharepoint = readers.mail, readers.bc, dict(readers.sharepoint)
    ingestor = Ingestor(args.state_dir, sources, actor=actor, mirror=mirror, mail=mail,
                        sharepoint=sharepoint, bc=bc, mc=mc)
    return ingestor, runtime


def _mc_reader(args: argparse.Namespace, environ: Mapping[str, str], tenant: str,
               sources: Sequence[dict[str, Any]]) -> Any:
    """The MC capture reader from the environment (MC_MCP_API_KEY, MC_OPERATOR_EMAIL)
    and the live audience, which decides who may read each ``mc:<scope>:<collection>``
    and, for tasks, approvals,
    task events and buckets, each record's ``mc:<scope>:project:<project>`` as well
    (MC-shared and ``unparented`` projects included: a reader needs both). Without MC
    credentials every ``mc_sources`` source is ``not_configured``."""
    from integrations.http import SourceUnavailable
    from integrations.mc_sources import CaptureMcClient, McCaptureReader
    from integrations.plx_hub_guard import HubTransport, McConfig

    if args.live_audience is None:
        raise IngestError("ingest_live_audience_required")
    audience = load_live_audience(args.live_audience, environ)
    if audience.tenant != tenant:
        raise IngestError("ingest_live_audience_tenant_mismatch")
    hub = HubTransport(environ=environ)
    try:
        mc = CaptureMcClient(McConfig.from_env(environ), hub=hub)
    except SourceUnavailable:
        for spec in sources:
            if spec["kind"] == "mc_sources" and not spec["not_configured"]:
                spec["not_configured"] = sorted(
                    name for name in ("MC_MCP_API_KEY", "MC_OPERATOR_EMAIL")
                    if not (environ.get(name) or "").strip())
        return None
    return McCaptureReader(args.state_dir, audience, mc)


def _regressions(previous: Mapping[str, Any] | None, current: Mapping[str, Any] | None) -> int:
    """1 when a time-ordered cursor value moved backwards between runs, else 0."""
    if not previous or not current:
        return 0
    for key in ("received_high_water", "high_water_modified"):
        old, new = _instant(previous.get(key)), _instant(current.get(key))
        if old is not None and (new is None or new < old):
            return 1
    for key in ("scans", "traversals_started"):
        if isinstance(previous.get(key), int) and (current.get(key) or 0) < previous[key]:
            return 1
    return 0


def main(argv: Sequence[str] | None = None, *, environ: Mapping[str, str] | None = None) -> int:
    env = os.environ if environ is None else environ
    parser = argparse.ArgumentParser(prog="python3 -m operating.ingestion",
                                     description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="advance every configured source once (counts only)")
    run.add_argument("--state-dir", required=True, type=Path)
    run.add_argument("--sources", required=True, type=Path)
    run.add_argument("--live-audience", type=Path)
    run.add_argument("--mirror-audience", type=Path)
    run.add_argument("--runs", type=int, default=1, help="consecutive runs (1..3)")
    run.add_argument("--only", action="append")
    run.add_argument("--base-url-env", default="PLX_WORKBENCH_API_URL")
    run.add_argument("--key-env", default="PLX_WORKBENCH_API_KEY")
    show = commands.add_parser("status", help="counts-only status")
    show.add_argument("--state-dir", required=True, type=Path)
    show.add_argument("--sources", type=Path)
    check = commands.add_parser("validate", help="validate a sources file offline")
    check.add_argument("--sources", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "validate":
            tenant, actor, sources = load_sources_file(args.sources, env)
            print(json.dumps({"schema": SOURCES_SCHEMA, "valid": True,
                              "sources": len(sources),
                              "by_kind": {k: sum(s["kind"] == k for s in sources) for k in KINDS},
                              "not_configured": sum(bool(s["not_configured"]) for s in sources)},
                             indent=1, sort_keys=True))
            return 0
        if args.command == "status":
            sources = load_sources_file(args.sources, env)[2] if args.sources else []
            report = status(args.state_dir, sources=sources)
            text = json.dumps(report, indent=1, sort_keys=True)
            report["row_data_check"] = row_data_check(args.state_dir, text)
            print(json.dumps(report, indent=1, sort_keys=True))
            return 0
        if not 1 <= args.runs <= 3:
            raise IngestError("ingest_runs_invalid")
        ingestor, runtime = _build(args, env)
        try:
            runs, regressions = [], 0
            for _ in range(args.runs):
                cursors_before = {row["unit_id"]: json.loads(row["cursor_json"]) for row in
                                  ingestor.store.db.execute("SELECT * FROM unit_cursor")}
                result = ingestor.run(only=args.only)
                for unit in result["units"]:
                    regressions += _regressions(cursors_before.get(unit["unit_id"]),
                                                ingestor.store.cursor(unit["unit_id"]))
                runs.append({"run_id": result["run_id"], "summary": result["summary"],
                             "units": [{k: u[k] for k in ("unit_id", "state", "reasons",
                                                          "counts")}
                                       for u in result["units"]]})
            report = {"schema": RUN_SCHEMA, "reader": "environment", "runs": runs,
                      "cursor_regressions": regressions,
                      "status": status(args.state_dir, sources=ingestor.sources)}
            blocked = blocked_upstream(runs[-1]["units"])
            if blocked:
                report["blocked_upstream"] = blocked
        finally:
            ingestor.close()
            if runtime is not None:
                runtime.close()
        text = json.dumps(report, indent=1, sort_keys=True)
        report["row_data_check"] = row_data_check(args.state_dir, text)
        print(json.dumps(report, indent=1, sort_keys=True))
        return 0
    except IngestError as exc:
        print(json.dumps({"error": exc.code}), file=sys.stderr)
        return 1


__all__ = ["IngestError", "IngestStore", "Ingestor", "KINDS", "live_audience_sites",
           "load_live_audience", "load_sources", "load_sources_file", "main", "resolve_sites",
           "row_data_check", "status"]


if __name__ == "__main__":
    raise SystemExit(main())
