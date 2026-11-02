"""Versioned SharePoint list-item capture (L1): the items of the lists a scope names.

A generic capture of the items of SharePoint lists that are not
document libraries, its scope data (``integrations/sharepoint_list_capture_scope.json``,
``plx-sharepoint-list-capture-scope/v1``), naming first only lists whose data exists nowhere
else: the Vendor Quotes list the Portal's RFQ form writes (plx-customer-portal@31a2623
``portal/src/app/api/rfq/submit/route.ts``:5,176). A list is named by its Graph site id and
list id with the columns to read; nothing else is listed or discovered.

Reads (GET only, through the one guarded Graph client ``integrations.graph_read
.build_graph``; grammar operations ``graph.site_list``, ``graph.list_items``,
``graph.list_item_permissions`` and ``graph.list_item_permissions_beta``), per list and
run, after the capture principal's live audience grants the list (no I/O otherwise):

* the list itself (``$select=id,list``): it must be the list the scope names and carry a
  ``list.template`` other than ``documentLibrary`` (a library's files are the drive
  capture's, ``integrations.capture_sharepoint``); else nothing of it is read;
* its items, ``$expand=fields($select=<the scope's columns>)``, page by page (``$top``
  ``page_size``) through server ``@odata.nextLink``s the guard keeps inside the collection;
  at most ``max_pages_per_run`` pages and ``max_items_per_run`` items per run: a run stopped
  there keeps its cursor and the next run resumes it (``cap:page_cap``/``cap:item_cap``); an
  expired continuation (HTTP 410) restarts the listing once;
* each item's own permissions (``graph_read.permission_acl``, the drive capture's reading),
  kept as the version's ``source_acl``: the v1.0 item read first; once it is refused (an
  HTTP 4xx other than 408/429: v1.0 may not serve listItem permissions app-only) the
  documented beta read of the same item (``/beta/sites/{id}/lists/{id}/items/{id}
  /permissions``, Sites.Read.All app) for the rest of the list's run, never a list- or
  site-wide ACL (the grammar parses none). The first 401/403 (a blocked part
  ``sharepoint_list.item_permissions``) or 404 (``acl_endpoint_not_found``) of the beta read
  stops permission reads for the whole list in the run; such items, and any whose read
  failed, are kept with an ``unreadable`` ACL (served to no reader the audience requires a
  source ACL of, so to no employee). A read that failed transiently (408/429/5xx or no
  answer) is read once more at the end of the list's listing, after a pause; an item read
  then is saved again with its permissions (``acl_recovered``), one still failing stays
  unreadable.

Records (``integrations.capture_store.CaptureStore``, the store Teams and BC use): kind
``sharepoint_list``, path (Graph site id, list id, item id), cited
``sharepoint-list://<site>/<list>/<item>@<generation>-<capture-id>``, audience unit
``sharepoint-list:<site>/<list>`` and live-audience resource
``sharepoint:list:<site>:<list>``. ``row`` is the Graph listItem JSON the generated
``SharePointListItem`` type reads (``ontology/schema/generated/graph_sharepoint_lists.json``):
id, eTag, times, createdBy/lastModifiedBy, webUrl, contentType, ``parentReference.siteId`` and
``sharepointIds.listId`` (the scope's identities, the request's collection) and ``fields``
(only the scope's columns; numbers as exact decimal text). An item of another site, without
a list item id or eTag, without fields or with fields over 64 kB is dropped and counted
(``provenance_dropped``, never complete); folders are counted, not captured. One version per
content: an eTag or permission change is a new version.

Permission trimming (every read path of the store): a reader needs the live audience grant
of ``sharepoint:list:<site>:<list>`` and, where the audience requires a source ACL (always
for an employee: ``operating.entitlements.ACL_BEARING_PATTERNS`` ``sharepoint:*``), one of
its identities in the version's captured item permissions *and* in the item's current
version's (``SourceKind.current_acl``), so a reader removed from an item loses its history
too. Complete means the latest run listed the whole list with nothing dropped and every
listed item's permissions read, those saved before a resume included (else the run is
finished ``partial``, ``acl_unreadable`` or the blocked part's reason);
``deletions_proven`` is never claimed (``finish_run`` marks an item no longer listed
``missing_from_source_since``).

Discovery (coverage audit 2026-10-30: one list captured, the lists available never
counted). A scope's optional ``discover`` block (``{"sites": "all_tenant_sites",
"exclude_lists": [list ids], "max_sites", "max_lists"}``) lists every site the
application may read (``graph.sites_all``, ``graph_read.SharePointSites``), OneDrives
left out (a person's site is read only when a scope names its list: no person's lists
are enumerated past the people guard), then each site's lists (``graph.site_lists``,
``$select=id,displayName,list,system``) and, for each list that is no library
(``list.template`` ending ``Library``: the drive capture's), not hidden and not a
system list and whose audience grant the capture principal holds (checked first: a list
not granted has nothing read, its columns neither, ``lists_not_visible``), its columns
(``graph.list_columns``): every column not hidden and not one
of SharePoint's own bookkeeping columns (``SYSTEM_COLUMNS``), at most 100. Each list
found is then captured exactly as a named one (audience grant per list first, item
permissions per item). A named list keeps its own columns. ``discovery`` counts what
was seen, left out and why; a site whose lists could not be read is counted
(``sites_failed``) and keeps discovery incomplete, never silently empty. The CLI's
``--discover-only`` lists nothing but the counts of what a run would capture.

Storage: ``<state-dir>/sharepoint-list-capture/capture.db`` (private modes), outside every
git work tree. Live-unverified until live check ``capture.sharepoint_list_items``
passes: whether the application's grant reads the personal site the list lives on and
answers listItem permissions app-only.

CLI (counts, booleans and enums only)::

    python3 -m integrations.graph_read sharepoint-list-capture --state-dir D \\
        --live-audience examples/sharepoint_list_capture_audience.live-check.json --runs 2
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import time
from typing import Any, Callable, Mapping, Sequence

from ontology.types import Actor

from . import live_source_grammar as grammar
from .capture_mail import assert_outside_git, blocked_reason, graph_block
from .capture_store import (CaptureError, CaptureStore, CapturedRecord, SourceKind,
                            register_source_kind)
from .graph_read import permission_acl
from .live_source_guard import (LiveSourceAudience, SourceUnavailable, jsonable,
                                nextlink_refused, unavailable_detail)

SCOPE_SCHEMA = "plx-sharepoint-list-capture-scope/v1"
STORE_DIR = "sharepoint-list-capture"
DB_FILE = "capture.db"
LINEAGE = "sharepoint"
RELATION = "listItems"   # the ontology relation of its records (source family sharepoint)
REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SCOPE = REPO_ROOT / "integrations" / "sharepoint_list_capture_scope.json"
CAPTURE_PRINCIPAL = "svc:sharepoint-list-capture"
# The Graph application permission the reads need, named when Graph refuses one (403).
GRAPH_PERMISSIONS = ("Sites.Read.All",)
_PERMISSION = {"graph.site_list": "Sites.Read.All", "graph.list_items": "Sites.Read.All",
               "graph.list_item_permissions": "Sites.Read.All",
               "graph.list_item_permissions_beta": "Sites.Read.All",
               "graph.sites_all": "Sites.Read.All", "graph.site_lists": "Sites.Read.All",
               "graph.list_columns": "Sites.Read.All"}
ACL_V1, ACL_BETA = "graph.list_item_permissions", "graph.list_item_permissions_beta"
MAX_FIELDS_BYTES = 64_000   # the bound of graph_read.SharePointReader.list_item_fields
REASON_MAX = 64
# The audience unit (C0 audience_key) of a list: its own prefix, so a reader policy that
# decides ``sharepoint:<drive>`` units by the drive capture never decides a list.
UNIT_PREFIX = "sharepoint-list:"
_ITEM = re.compile(r"[1-9][0-9]{0,9}")
_ROW_KEYS = ("createdDateTime", "lastModifiedDateTime", "createdBy", "lastModifiedBy", "webUrl",
             "contentType")
_LIMITS = {"page_size": (200, 1, 999), "max_pages_per_run": (50, 1, 500),
           "max_items_per_run": (5000, 1, 20_000)}
_RESYNC_CODES = frozenset({"resync_required", "sync_state_not_found"})
_DISCOVER_LIMITS = {"max_sites": (500, 1, 5000), "max_lists": (500, 1, 5000)}
# SharePoint's own bookkeeping columns: never a list's content (its times and authors are
# the item's createdDateTime/createdBy row keys already).
SYSTEM_COLUMNS = frozenset({
    "ContentType", "Attachments", "Edit", "LinkTitle", "LinkTitleNoMenu", "DocIcon",
    "ItemChildCount", "FolderChildCount", "AppAuthor", "AppEditor", "ComplianceAssetId",
    "_ComplianceFlags", "_ComplianceTag", "_ComplianceTagWrittenTime",
    "_ComplianceTagUserId", "_IsRecord", "_UIVersionString", "Created", "Modified",
    "Author", "Editor", "ID", "_ColorTag", "SelectTitle", "SelectFilename", "_CopySource",
    "_HasCopyDestinations", "_ModerationComments", "_ModerationStatus", "Order",
    "GUID", "WorkflowVersion", "WorkflowInstanceID", "FileRef", "FileDirRef", "FSObjType",
    "PermMask", "UniqueId", "ProgId", "ScopeId", "MetaInfo", "owshiddenversion",
    "_Level", "_IsCurrentVersion", "Restricted", "OriginatorId", "NoExecute",
    "ContentVersion", "_VirusStatus", "_VirusVendorID", "_VirusInfo", "SMTotalSize",
    "SMLastModifiedDate", "SMTotalFileStreamSize", "SMTotalFileCount", "AccessPolicy",
    "_CommentFlags", "_CommentCount", "_DisplayName", "BSN", "InstanceID",
    "ParentVersionString", "ParentLeafName", "TemplateUrl", "xd_ProgID",
    "xd_Signature", "HTML_x0020_File_x0020_Type", "_SourceUrl", "_SharedFileIndex",
    "LinkFilenameNoMenu", "LinkFilename", "LinkFilename2", "File_x0020_Type",
    "_EditMenuTableStart", "_EditMenuTableStart2", "_EditMenuTableEnd",
    "ServerUrl", "EncodedAbsUrl", "BaseName", "SyncClientId", "_CheckinComment",
    "CheckoutUser", "CheckedOutUserId", "IsCheckedoutToLocal", "CheckedOutTitle",
    "_HasEncryptedContent", "_StubFile", "_ShortcutUrl", "_ShortcutSiteId",
    "_ShortcutWebId", "_ShortcutUniqueId", "MediaServiceFastMetadata"})
MAX_DISCOVERED_COLUMNS = 100


# An item permission read that failed this way is re-read once at the end of its list's
# listing, after a pause (``ListCapture._reread_transient``).
TRANSIENT_ACL_STATUSES = frozenset({408, 429, 500, 502, 503, 504})
ACL_RETRY_PAUSE_SECONDS = 10.0
RETRY_BUDGET_SECONDS = 240   # the run's GET retry sleep, within the host's list timeout


def _v1_refused(status: Any) -> bool:
    """A v1.0 item-permissions answer that sends the list to the beta read: a refusal of
    the request itself (4xx), not a throttle or timeout."""
    return type(status) is int and 400 <= status < 500 and status not in (408, 429)


class ListCaptureError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _valid(type_name: str, value: Any) -> bool:
    return grammar.TYPES[type_name].valid(value)


def _list_path(parts: Sequence[str]) -> tuple[str, ...]:
    if isinstance(parts, (str, bytes)) or len(parts) != 3 \
            or not all(isinstance(p, str) for p in parts):
        raise ValueError("sharepoint_list_path_invalid")
    site, list_id, item = parts[0].lower(), parts[1].lower(), parts[2]
    if not (_valid("site_id", site) and _valid("guid", list_id) and _ITEM.fullmatch(item)):
        raise ValueError("sharepoint_list_path_invalid")
    return site, list_id, item


def _resource(path: tuple[str, ...]) -> str:
    return f"sharepoint:list:{path[0]}:{path[1]}"


LIST_KIND = register_source_kind(SourceKind(
    name="sharepoint_list", scheme="sharepoint-list", path_parts=3, canonical_path=_list_path,
    unit=lambda path: f"{UNIT_PREFIX}{path[0]}/{path[1]}",
    resources=lambda path: (_resource(path),),
    key=lambda path: {"site_id": path[0], "list_id": path[1], "id": path[2]},
    current_acl=True))


# ------------------------------------------------------------------ scope

@dataclass(frozen=True)
class ListTarget:
    """One list of the scope: its Graph site id, list id and the columns read."""

    name: str
    site_id: str
    list_id: str
    columns: tuple[str, ...]

    def path(self, item_id: str) -> tuple[str, ...]:
        return LIST_KIND.canonical_path([self.site_id, self.list_id, item_id])

    @property
    def unit(self) -> str:
        return LIST_KIND.unit(self.path("1"))

    @property
    def resource(self) -> str:
        return LIST_KIND.resources(self.path("1"))[0]

    def url(self, op: str, query: Mapping[str, str] | None = None, **params: str) -> str:
        return grammar.url(op, query, site=self.site_id, list=self.list_id, **params)

    def items_url(self, page_size: int) -> str:
        return self.url("graph.list_items", {
            "$expand": "fields($select=" + ",".join(self.columns) + ")",
            "$top": str(page_size)})

    def same_site(self, value: Any) -> bool:
        """Whether an item's ``parentReference.siteId`` names this list's site: the
        composite id, or its site collection id alone."""
        given = value.lower() if isinstance(value, str) else None
        return given in (self.site_id, self.site_id.split(",")[1] if "," in self.site_id
                         else self.site_id)


class ListScope:
    """``plx-sharepoint-list-capture-scope/v1``: ``lists`` (``{name, site_id, list_id,
    columns}``, optional ``evidence`` and ``note``), the optional ``discover`` block
    (module docstring, Discovery) and the per-run caps of ``_LIMITS``. Anything unknown,
    duplicated or out of bounds is refused (``ListCaptureError``)."""

    def __init__(self, document: Any) -> None:
        if not isinstance(document, Mapping) or document.get("schema") != SCOPE_SCHEMA \
                or set(document) - {"schema", "note", "lists", "discover", *_LIMITS}:
            raise ListCaptureError("scope_invalid")
        self.discover = _discover_config(document.get("discover"))
        entries = document.get("lists")
        if not isinstance(entries, list) or not 1 <= len(entries) <= 50:
            raise ListCaptureError("scope_lists_invalid")
        targets: list[ListTarget] = []
        for entry in entries:
            if not isinstance(entry, Mapping) or set(entry) - {
                    "name", "site_id", "list_id", "columns", "evidence", "note"}:
                raise ListCaptureError("scope_list_invalid")
            name, site, list_id, columns = (entry.get(k) for k in (
                "name", "site_id", "list_id", "columns"))
            if (not isinstance(name, str) or not name.strip() or len(name) > 200
                    or not _valid("site_id", site) or not isinstance(list_id, str)
                    or not _valid("guid", list_id.lower()) or not isinstance(columns, list)
                    or not 1 <= len(columns) <= 100 or len(set(columns)) != len(columns)
                    or not all(grammar.column_name_ok(c) for c in columns)):
                raise ListCaptureError("scope_list_invalid")
            targets.append(ListTarget(name, site.lower(), list_id.lower(), tuple(columns)))
        if len({t.unit for t in targets}) != len(targets):
            raise ListCaptureError("scope_list_duplicate")
        self.lists = tuple(targets)
        for key, (default, low, high) in _LIMITS.items():
            value = document.get(key, default)
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise ListCaptureError(f"scope_{key}_invalid")
            setattr(self, key, value)

    @classmethod
    def from_file(cls, path: str | Path) -> "ListScope":
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))


def _discover_config(value: Any) -> dict[str, Any] | None:
    """The scope's ``discover`` block, checked; None when absent."""
    if value is None:
        return None
    if not isinstance(value, Mapping) or set(value) - {
            "sites", "exclude_lists", "note", *_DISCOVER_LIMITS} \
            or value.get("sites") != "all_tenant_sites":
        raise ListCaptureError("scope_discover_invalid")
    excluded = value.get("exclude_lists", [])
    if not isinstance(excluded, list) or not all(
            isinstance(e, str) and _valid("guid", e.lower()) for e in excluded):
        raise ListCaptureError("scope_discover_invalid")
    config: dict[str, Any] = {"exclude_lists": frozenset(e.lower() for e in excluded)}
    for key, (default, low, high) in _DISCOVER_LIMITS.items():
        found = value.get(key, default)
        if isinstance(found, bool) or not isinstance(found, int) or not low <= found <= high:
            raise ListCaptureError(f"scope_discover_{key}_invalid")
        config[key] = found
    return config


def _new_discovery() -> dict[str, Any]:
    return {"status": "not_requested", "sites_listed": 0, "sites_personal_skipped": 0,
            "sites_read": 0, "sites_failed": 0, "lists_seen": 0, "lists_library": 0,
            "lists_hidden_or_system": 0, "lists_excluded": 0, "lists_named": 0,
            "lists_without_columns": 0, "lists_columns_failed": 0, "lists_discovered": 0,
            "lists_not_visible": 0,
            "lists_over_cap": 0, "provenance_dropped": 0, "reasons": []}


# ------------------------------------------------------------------ capture

def _failure(part: str, op: str, exc: BaseException, blocked: list[dict[str, Any]]) -> str:
    """``blocked:<code>[:<permission>]`` where the one Graph refusal rule
    (``capture_mail.graph_block``) blocks (the part added to ``blocked``), else
    ``source_error:<Class>[:<detail>]`` (the Teams capture's reasons)."""
    found = graph_block(part, op, _PERMISSION, getattr(exc, "http_status", None))
    if found is not None:
        blocked.append(found)
        return blocked_reason(found)
    reason, detail = f"source_error:{type(exc).__name__}", unavailable_detail(exc)
    if detail:
        reason = (reason if len(f"{reason}:{detail}") <= REASON_MAX
                  else "source_error:SourceUnavailable") + ":" + detail
    return reason


def _new_counts() -> dict[str, int]:
    return {"pages": 0, "items_read": 0, "versions_new": 0, "unchanged": 0,
            "provenance_dropped": 0, "folders": 0, "acl_read": 0, "acl_read_beta": 0,
            "acl_unreadable": 0, "round_restarts": 0, "acl_transient": 0,
            "acl_recovered": 0}


def item_row(target: ListTarget, raw: Any) -> dict[str, Any] | str | None:
    """The captured row of one listed item, ``"folder"`` for a folder, or None when it does
    not belong here or breaks a bound (dropped)."""
    if not isinstance(raw, Mapping):
        return None
    item_id, etag, fields = raw.get("id"), raw.get("eTag"), raw.get("fields")
    parent = raw.get("parentReference")
    if (not isinstance(item_id, str) or not _ITEM.fullmatch(item_id)
            or not isinstance(etag, str) or not etag or len(etag) > 200
            or isinstance(parent, Mapping) and parent.get("siteId") is not None
            and not target.same_site(parent.get("siteId"))):
        return None
    kind = raw.get("contentType")
    kind = kind.get("id") if isinstance(kind, Mapping) else None
    if isinstance(kind, str) and kind.upper().startswith("0X0120"):
        return "folder"
    if not isinstance(fields, Mapping):
        return None
    kept = jsonable({name: fields[name] for name in target.columns if name in fields})
    try:
        encoded = json.dumps(kept, sort_keys=True, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        return None
    if len(encoded.encode("utf-8")) > MAX_FIELDS_BYTES:
        return None
    return {"id": item_id, "eTag": etag,
            **{key: jsonable(raw[key]) for key in _ROW_KEYS if key in raw},
            "parentReference": {"siteId": target.site_id},
            "sharepointIds": {"listId": target.list_id, "listItemId": item_id},
            "fields": kept}


def open_store(state_dir: str | Path, audience: Any) -> CaptureStore:
    """The list-item capture store under ``state_dir`` (outside every git work tree)."""
    root = assert_outside_git(state_dir) / STORE_DIR
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    store = CaptureStore(root / DB_FILE, audience)
    os.chmod(root / DB_FILE, 0o600)
    return store


class ListCapture:
    """One capture pass over a ``ListScope`` into a ``CaptureStore``, as ``actor``
    (checked against the live audience per list before any of its I/O)."""

    def __init__(self, store: CaptureStore, api: Any, audience: LiveSourceAudience,
                 actor: Actor, *, sleep: Callable[[float], None] = time.sleep) -> None:
        self.store, self.api, self.audience, self.actor = store, api, audience, actor
        self.targets: tuple[ListTarget, ...] = ()   # the lists the last run took
        self._sleep = sleep

    def run(self, scope: ListScope, *, discover_only: bool = False) -> dict[str, Any]:
        blocked: list[dict[str, Any]] = []
        found, discovery = self.discover(scope, blocked)
        self.targets = scope.lists + tuple(found)
        results = ([] if discover_only else
                   [self.capture(target, scope, blocked) for target in self.targets])
        unique = list({json.dumps(p, sort_keys=True): p for p in blocked}.values())
        return {"complete": discovery["status"] in ("not_requested", "complete")
                and all(r["status"] == "complete" for r in results), "lists": results,
                "blocked_upstream": unique, "discovery": discovery,
                "complete_bound": sum(r["status"] == "complete" for r in results),
                "bound_total": len(results)}

    def discover(self, scope: ListScope, blocked: list[dict[str, Any]]
                 ) -> tuple[list[ListTarget], dict[str, Any]]:
        """The lists the scope's ``discover`` block finds beyond its named ones (module
        docstring, Discovery), each with its readable columns; and the counts."""
        from .graph_read import SharePointSites

        report = _new_discovery()
        config = scope.discover
        if config is None:
            return [], report
        reasons: set[str] = set()
        named = {(t.site_id, t.list_id) for t in scope.lists}
        # The tenant's site host, from a named list's (its OneDrive host maps to it).
        host = scope.lists[0].site_id.split(",", 1)[0].replace("-my.sharepoint.com",
                                                                ".sharepoint.com")
        try:
            sites, completeness = SharePointSites(self.api, host).sites(
                max_items=config["max_sites"])
        except SourceUnavailable as exc:
            report.update(status="incomplete",
                          reasons=[_failure("sharepoint_list.sites", "graph.sites_all", exc,
                                            blocked)])
            return [], report
        if completeness.get("truncated"):
            reasons.add(f"cap:{completeness.get('truncation_reason') or 'sites'}")
        report["sites_listed"] = len(sites)
        report["provenance_dropped"] += completeness.get("provenance_dropped", 0)
        found: list[ListTarget] = []
        for site in sites:
            if site["personal"] is not None or "-my.sharepoint.com" in site["host"]:
                report["sites_personal_skipped"] += 1
                continue
            try:
                lists, done = self.api.get_pages(grammar.url(
                    "graph.site_lists", {"$select": "id,displayName,list,system"},
                    site=site["id"]), max_pages=10, max_items=2000)
            except SourceUnavailable as exc:
                report["sites_failed"] += 1
                reasons.add(_failure("sharepoint_list.site_lists", "graph.site_lists", exc,
                                     blocked))
                continue
            report["sites_read"] += 1
            if done.get("truncated"):
                reasons.add("cap:site_lists")
            for raw in lists:
                report["lists_seen"] += 1
                list_id = str(raw.get("id") or "").lower()
                facet = raw.get("list") if isinstance(raw.get("list"), Mapping) else {}
                template = facet.get("template")
                name = raw.get("displayName")
                if not _valid("guid", list_id) or not isinstance(name, str) or not name.strip():
                    report["provenance_dropped"] += 1
                    continue
                if not isinstance(template, str) or template.endswith("Library"):
                    report["lists_library"] += 1
                    continue
                if facet.get("hidden") or raw.get("system") is not None:
                    report["lists_hidden_or_system"] += 1
                    continue
                if list_id in config["exclude_lists"]:
                    report["lists_excluded"] += 1
                    continue
                if (site["id"].lower(), list_id) in named:
                    report["lists_named"] += 1
                    continue
                if len(found) >= config["max_lists"]:
                    report["lists_over_cap"] += 1
                    reasons.add("cap:max_lists")
                    continue
                resource = f"sharepoint:list:{site['id'].lower()}:{list_id}"
                if not self.audience.allows(self.actor, resource):
                    # Not granted: nothing of it is read, its columns neither; it is
                    # reported not_visible by its capture (no I/O there either).
                    report["lists_not_visible"] += 1
                    found.append(ListTarget(name.strip()[:200], site["id"].lower(), list_id,
                                            ("Title",)))
                    continue
                columns = self._columns(site["id"].lower(), list_id, report)
                if columns is None:
                    reasons.add("list_columns_unread")
                    continue
                if not columns:
                    report["lists_without_columns"] += 1
                    continue
                found.append(ListTarget(name.strip()[:200], site["id"].lower(), list_id,
                                        columns))
        report["lists_discovered"] = len(found)
        if report["sites_failed"]:
            reasons.add("sites_failed")
        report["reasons"] = sorted(reasons)
        report["status"] = "incomplete" if reasons else "complete"
        return found, report

    def _columns(self, site_id: str, list_id: str, report: dict[str, Any]
                 ) -> tuple[str, ...] | None:
        """A discovered list's readable columns (module docstring); None when the column
        read failed (counted)."""
        try:
            raws, _done = self.api.get_pages(grammar.url(
                "graph.list_columns", {"$select": "name,hidden"}, site=site_id,
                list=list_id), max_pages=5, max_items=1000)
        except SourceUnavailable:
            report["lists_columns_failed"] += 1
            return None
        names = []
        for raw in raws:
            name = raw.get("name")
            if (isinstance(name, str) and not raw.get("hidden") and name not in SYSTEM_COLUMNS
                    and grammar.column_name_ok(name) and name not in names):
                names.append(name)
        return tuple(sorted(names)[:MAX_DISCOVERED_COLUMNS])

    def capture(self, target: ListTarget, scope: ListScope,
                blocked: list[dict[str, Any]]) -> dict[str, Any]:
        counts = _new_counts()
        result: dict[str, Any] = {"unit": target.unit, "status": "incomplete", "reasons": [],
                                  "resumed_from": None, "counts": counts}
        if not self.audience.allows(self.actor, target.resource):
            return self._result(result, {"not_visible"})   # no I/O for a withheld list
        try:
            listed = self.api.get_json(target.url("graph.site_list", {"$select": "id,list"}))
        except SourceUnavailable as exc:
            return self._result(result, {_failure("sharepoint_list.list", "graph.site_list",
                                                  exc, blocked)})
        facet = listed.get("list") if isinstance(listed, Mapping) else None
        if not isinstance(listed, Mapping) or str(listed.get("id") or "").lower() \
                != target.list_id:
            return self._result(result, {"list_identity_mismatch"})
        template = facet.get("template") if isinstance(facet, Mapping) else None
        if not isinstance(template, str) or template == "documentLibrary":
            return self._result(result, {"document_library" if template else
                                         "list_template_unknown"})
        run = self.store.begin_run(LIST_KIND.name, target.unit, [target.resource])
        run_id, reasons = run["run_id"], set()
        acl_state: dict[str, Any] = {"op": ACL_V1, "blocked": None, "transient": {}}
        url = target.items_url(scope.page_size)
        if run["resumed"] and run["cursor"]:
            url, result["resumed_from"] = run["cursor"], "cursor"
        pages = items = 0
        restarted = False
        while True:
            if pages >= scope.max_pages_per_run or items >= scope.max_items_per_run:
                cap = "page_cap" if pages >= scope.max_pages_per_run else "item_cap"
                self.store.pause_run(run_id, reason=f"cap:{cap}")
                return self._result(result, reasons | self._acl_reasons(acl_state, counts)
                                    | {f"cap:{cap}"})
            try:
                page = self.api.get_json(url)
                if not isinstance(page, Mapping) or not isinstance(page.get("value"), list):
                    raise SourceUnavailable("graph listing lacks value list")
                next_url = self._next(url, page.get("@odata.nextLink"))
            except SourceUnavailable as exc:
                if (getattr(exc, "http_status", None) == 410
                        or getattr(exc, "error_code", None) in _RESYNC_CODES) and not restarted:
                    restarted = True   # an expired continuation: the listing starts over once
                    counts["round_restarts"] += 1
                    self.store.restart_run(run_id, "cursor_expired")
                    url = target.items_url(scope.page_size)
                    continue
                failed = _failure("sharepoint_list.items", "graph.list_items", exc, blocked)
                self.store.pause_run(run_id, reason=failed)
                return self._result(result, reasons | self._acl_reasons(acl_state, counts)
                                    | {failed})
            records, dropped = [], 0
            for raw in page["value"]:
                row = item_row(target, raw)
                if row == "folder":
                    counts["folders"] += 1
                    continue
                if row is None:
                    dropped += 1
                    continue
                acl = self._acl(target, row["id"], acl_state, blocked, counts)
                records.append(CapturedRecord(
                    target.path(row["id"]),
                    {"row": row, "meta": {"source_acl": acl, "source_api": "graph.list_items"}},
                    row["eTag"], LINEAGE))
            saved = self.store.save_page(run_id, records, next_url, dropped=dropped)
            counts["pages"] += 1
            counts["items_read"] += len(records)
            counts["versions_new"] += saved["new_versions"]
            counts["unchanged"] += saved["unchanged"]
            counts["provenance_dropped"] += dropped
            pages += 1
            items += len(records)
            if next_url is None:
                break
            url = next_url
        self._reread_transient(target, run_id, acl_state, blocked, counts)
        if (self.store.run(LIST_KIND.name, target.unit) or {}).get("dropped"):
            reasons.add("provenance_dropped")
        acl = self._acl_reasons(acl_state, counts)
        if not acl and any(((r.payload.get("meta") or {}).get("source_acl") or {})
                           .get("status") != "read"
                           for r in self.store.current_records(LIST_KIND.name, target.unit,
                                                               seen_in_run=run_id)):
            acl = {"acl_unreadable"}   # an item this run saved before a resume, unreadable then
        self.store.finish_run(run_id, "partial" if acl else "complete",
                              reason=min(acl) if acl else None)
        return self._result(result, reasons | acl)

    def _acl(self, target: ListTarget, item_id: str, state: dict[str, str | None],
             blocked: list[dict[str, Any]], counts: dict[str, int]) -> dict[str, Any]:
        """The item's own permissions as its source ACL (v1.0, else beta once v1.0 refused
        the read); ``unreadable`` (granting nobody who needs one) when the read fails or
        the beta read was refused or not found earlier in this list's run."""
        unreadable = {"status": "unreadable", "identities": []}
        while state["blocked"] is None:
            op = state["op"]
            try:
                perms, completeness = self.api.get_pages(target.url(op, item=item_id),
                                                         max_pages=3, max_items=200)
            except SourceUnavailable as exc:
                status = getattr(exc, "http_status", None)
                if op == ACL_V1 and _v1_refused(status):
                    state["op"] = ACL_BETA   # this item and the rest of the list's run
                    continue
                if op == ACL_BETA and status in (401, 403):
                    state["blocked"] = _failure("sharepoint_list.item_permissions", op, exc,
                                                blocked)
                elif op == ACL_BETA and status == 404:
                    state["blocked"] = "acl_endpoint_not_found"
                elif status is None or status in TRANSIENT_ACL_STATUSES:
                    # throttled or timed out (live 2026-11-01: 63 items of 6 lists were
                    # unreadable at capture and read cleanly on a later single read):
                    # re-read once at the end of the list's listing
                    counts["acl_transient"] += 1
                    transient = state.get("transient")
                    if isinstance(transient, dict):
                        transient[item_id] = True
                break
            acl = permission_acl(perms, completeness)
            counts["acl_read" if acl["status"] == "read" else "acl_unreadable"] += 1
            counts["acl_read_beta"] += op == ACL_BETA and acl["status"] == "read"
            return acl
        counts["acl_unreadable"] += 1
        return unreadable

    def _reread_transient(self, target: ListTarget, run_id: int, state: dict[str, Any],
                          blocked: list[dict[str, Any]], counts: dict[str, int]) -> None:
        """Re-read, once, after ``ACL_RETRY_PAUSE_SECONDS``, the permissions of every item
        whose permission read this listing failed transiently (throttled, 5xx, timeout).
        An item read now is saved again with the permissions it carries now (its version
        saved unreadable before stays as what the first read saw); one still failing stays
        unreadable (served to no reader who needs a source ACL). Nothing else is read: the
        rows are the run's own saved versions."""
        pending = list(state.get("transient") or ())
        state["transient"] = None   # a second failure is not queued again
        if not pending or state["blocked"] is not None:
            return
        current = {record.path[2]: record for record in self.store.current_records(
            LIST_KIND.name, target.unit, seen_in_run=run_id)}
        self._sleep(ACL_RETRY_PAUSE_SECONDS)
        records = []
        for item_id in pending:
            record = current.get(item_id)
            if record is None or state["blocked"] is not None:
                continue
            acl = self._acl(target, item_id, state, blocked, counts)
            # the item was counted unreadable by its first read; ``_acl`` counted this one
            counts["acl_unreadable"] -= 1
            if acl.get("status") != "read":
                continue
            counts["acl_recovered"] += 1
            records.append(CapturedRecord(
                record.path, {**record.payload,
                              "meta": {**(record.payload.get("meta") or {}),
                                       "source_acl": acl}},
                record.source_version, LINEAGE))
        if records:
            saved = self.store.save_page(run_id, records, None)
            counts["versions_new"] += saved["new_versions"]

    @staticmethod
    def _acl_reasons(state: Mapping[str, str | None], counts: Mapping[str, int]) -> set[str]:
        if state["blocked"]:
            return {state["blocked"]}
        return {"acl_unreadable"} if counts["acl_unreadable"] else set()

    def _next(self, url: str, link: Any) -> str | None:
        if link is None:
            return None
        found = self.api.continuation_url(url, link)
        if found is None or found == url:
            raise nextlink_refused("graph nextLink escaped the collection")
        return found

    @staticmethod
    def _result(result: dict[str, Any], reasons: set[str]) -> dict[str, Any]:
        result["reasons"] = sorted(reasons)
        result["status"] = ("blocked" if any(r.startswith("blocked:") for r in reasons)
                            else "incomplete" if reasons else "complete")
        return result


# ------------------------------------------------------------------ CLI (counts only)

def add_cli_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--state-dir", required=True,
                        help="Workbench state directory (outside every git work tree)")
    parser.add_argument("--scope", default=str(DEFAULT_SCOPE),
                        help="plx-sharepoint-list-capture-scope/v1 file")
    parser.add_argument("--live-audience", required=True,
                        help="plx-live-source-audience/v1 file granting the capture principal")
    parser.add_argument("--principal", default=CAPTURE_PRINCIPAL)
    parser.add_argument("--reader-principal", default="staff:list-reader",
                        help="a reader whose identities the captured item permissions grant: "
                             "what it sees must resolve offline")
    parser.add_argument("--outsider-principal", default="staff:list-outsider",
                        help="a reader granted the lists but by no item's permissions: it must "
                             "see nothing")
    parser.add_argument("--withheld-principal", default="staff:list-withheld",
                        help="a principal the audience does not grant")
    parser.add_argument("--runs", type=int, default=2,
                        help="capture runs; run 2+ resumes a stopped run from its cursor")
    parser.add_argument("--discover-only", action="store_true",
                        help="list the sites and lists a run would capture (counts only) "
                             "and read no list item")


def _read_back(store: CaptureStore, units: Sequence[str], actor: Actor
               ) -> tuple[list[str], int]:
    """(citations ``actor`` may see, items withheld from it) over ``units``."""
    seen, withheld = [], 0
    for unit in units:
        after = None
        while True:
            try:
                page = store.records(actor, LIST_KIND.name, unit, after_path=after, limit=1000)
            except CaptureError:
                break   # the list is not visible to this actor at all
            seen += [item["citation"] for item in page["items"]]
            withheld += page["withheld_count"]
            after = page["next_after_path"]
            if after is None:
                break
    return seen, withheld


def _report_run(number: int, run: Mapping[str, Any]) -> dict[str, Any]:
    return {"run": number, "complete": run["complete"], "discovery": dict(run["discovery"]),
            "complete_bound": run["complete_bound"], "bound_total": run["bound_total"],
            "lists": [{"index": i, "status": r["status"], "reasons": r["reasons"],
                       "resumed_from": r["resumed_from"], "counts": dict(r["counts"])}
                      for i, r in enumerate(run["lists"])]}


def run_cli(args: argparse.Namespace, *, environ: Mapping[str, str] | None = None,
            sender: Any = None, sleep: Callable[[float], None] | None = None) -> dict[str, Any]:
    """Capture ``--runs`` times, then read the store back as a reader the item permissions
    grant, an outsider (granted, in no item's permissions) and a withheld actor. Counts,
    booleans and enums only: never an id, name, address or value."""
    from .graph_read import build_graph
    from .live_source_guard import BoundedBackoff

    if isinstance(args.runs, bool) or not 1 <= args.runs <= 5:
        raise ValueError("runs must be 1..5")
    scope = ListScope.from_file(args.scope)
    audience = LiveSourceAudience.from_file(args.live_audience)
    actors = {name: Actor(audience.tenant, frozenset({principal}), "restricted")
              for name, principal in (("capture", args.principal),
                                      ("reader", args.reader_principal),
                                      ("outsider", args.outsider_principal),
                                      ("withheld", args.withheld_principal))}
    store = open_store(args.state_dir, audience)
    try:
        # The retry budget covers every GET of the run (some 3,500 item permission reads
        # over 17 lists live): 60 s ran out under throttling and left items unreadable.
        retry = BoundedBackoff(budget_seconds=RETRY_BUDGET_SECONDS,
                               **({"sleep": sleep} if sleep else {}))
        api, transport = build_graph(environ, sender=sender, retry=retry)
        capture = ListCapture(store, api, audience, actors["capture"],
                              **({"sleep": sleep} if sleep else {}))
        runs, blocked = [], []
        only = bool(getattr(args, "discover_only", False))
        for number in range(1, (1 if only else args.runs) + 1):
            run = capture.run(scope, discover_only=only)
            runs.append(_report_run(number, run))
            blocked += [p for p in run["blocked_upstream"] if p not in blocked]
        units = [t.unit for t in capture.targets
                 if store.run(LIST_KIND.name, t.unit) is not None]
        operator = [store.operator_completeness(actors["capture"], LIST_KIND.name, u)
                    for u in units]
        current = sum(o["records_current"] for o in operator)
        reader, reader_withheld = _read_back(store, units, actors["reader"])
        resolved = sum(1 for h in reader if store.resolve(h, actors["reader"]).get("ok"))
        outsider, outsider_withheld = _read_back(store, units, actors["outsider"])
        hidden, _ = _read_back(store, units, actors["withheld"])
        lists = runs[-1]["lists"]
        errors = sorted({r for item in lists for r in item["reasons"]
                         if r.startswith("source_error:")})
        report = {
            "check": "capture.sharepoint_list_items",
            "evidence_label": "injected-transport" if transport.sender_injected
            else "live-read",
            "read_only_guard": "GET-only; token POST only",
            "graph_permissions_required": list(GRAPH_PERMISSIONS),
            "storage": {"outside_git_tree": True,
                        "under_state_dir": store.path.parent.parent
                        == Path(args.state_dir).expanduser().resolve()},
            "status": ("blocked" if blocked else "complete" if runs[-1]["complete"]
                       else "incomplete"),
            "blocked_upstream": blocked,
            "runs": runs,
            "discover_only": only,
            "would_capture": {"lists_named": len(scope.lists),
                              "lists_discovered": runs[-1]["discovery"]["lists_discovered"],
                              "columns": sum(len(t.columns) for t in capture.targets)},
            "summary": {"lists": runs[-1]["bound_total"],
                        "lists_complete": runs[-1]["complete_bound"],
                        "units_captured": len(units), "items_current": current,
                        "versions": sum(o["versions_total"] for o in operator),
                        "acl_unreadable": sum(item["counts"]["acl_unreadable"]
                                              for item in lists),
                        "source_errors": len(errors)},
            "resolve_check": {"handles_checked": len(reader), "resolved_offline": resolved,
                              "reader_withheld": reader_withheld},
            "leak_check": {"outsider_actor_rows": len(outsider),
                           "outsider_actor_resolved": sum(
                               1 for h in reader if store.resolve(h, actors["outsider"])
                               .get("ok")),
                           "outsider_actor_withheld": outsider_withheld,
                           "withheld_actor_rows": len(hidden),
                           "withheld_actor_resolved": sum(
                               1 for h in reader if store.resolve(h, actors["withheld"])
                               .get("ok"))},
            # The ontology relation (source family sharepoint) this run holds records of.
            "relations": {RELATION: current} if current else {},
            "requests_sent": transport.requests_sent,
            "gets_sent": transport.gets_sent,
            # The Graph reads it made (grammar operations): the endpoints a pass evaluates.
            "operations_read": sorted(transport.operations_read),
        }
        leak = report["leak_check"]
        report["ok"] = bool(not blocked and not errors and resolved == len(reader)
                            and leak["outsider_actor_rows"] == 0
                            and leak["outsider_actor_resolved"] == 0
                            and leak["withheld_actor_rows"] == 0
                            and leak["withheld_actor_resolved"] == 0)
        return report
    finally:
        store.close()


__all__ = ["CAPTURE_PRINCIPAL", "DB_FILE", "GRAPH_PERMISSIONS", "LIST_KIND", "ListCapture",
           "ListCaptureError", "ListScope", "ListTarget", "RELATION", "SCOPE_SCHEMA",
           "STORE_DIR", "UNIT_PREFIX", "add_cli_arguments", "item_row", "open_store",
           "run_cli"]
