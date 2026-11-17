"""SharePoint capture scope: the configured targets, discovery rules and library classes a
capture walks, and the path and permission helpers they share (split from
``integrations.capture_sharepoint``, which re-exports every name)."""

from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any, Mapping, Sequence

from operating import doc_metadata_taxonomy as taxonomy
from . import live_source_grammar as grammar
from .capture_mail_scope import CaptureError
from .live_source_guard import MAX_CONTENT_BYTES as MAX_DOWNLOAD_BYTES


SCOPE_SCHEMA = "plx-sharepoint-capture-scope/v1"


FULL_WALK_HOURS = 24
PERMISSION_REREAD_HOURS = 24   # the default bound on a root's skipped permission re-reads
MAX_CONTENT_BYTES = 20_000_000   # a scope's default content cap: the document extractor's bound


# The largest content cap a scope may declare: the guard's one-download bound.
MAX_DECLARED_CONTENT_BYTES = MAX_DOWNLOAD_BYTES


_TARGET_ID = re.compile(r"^[a-z0-9][a-z0-9.-]{0,63}$")


def _under(path: str, root: str) -> bool:
    return root == "/" or path == root or path.startswith(root.rstrip("/") + "/")


def _in_root(path: str, root: str) -> bool:
    """Whether a listed path lies under a scope root (SharePoint paths are
    case-insensitive; the scope may spell the root in another case)."""
    return _under(path.casefold(), root.casefold())


class CaptureTarget:
    def __init__(self, entry: Mapping[str, Any]) -> None:
        allowed = {"id", "drive_id", "site", "folders", "max_depth", "max_folders", "note"}
        if not isinstance(entry, Mapping) or set(entry) - allowed:
            raise CaptureError("scope_target_invalid")
        self.id = entry.get("id")
        if not isinstance(self.id, str) or _TARGET_ID.fullmatch(self.id) is None:
            raise CaptureError("scope_target_invalid")
        drive, site = entry.get("drive_id"), entry.get("site")
        if (drive is None) == (site is None):
            raise CaptureError("scope_target_drive_or_site")
        if drive is not None and not grammar.TYPES["drive_id"].valid(drive):
            raise CaptureError("scope_drive_invalid")
        if site is not None and not grammar.TYPES["site_name"].valid(site):
            raise CaptureError("scope_site_invalid")
        self.drive_id, self.site = drive, site
        folders = entry.get("folders", ["/"])
        if isinstance(folders, (str, bytes)) or not isinstance(folders, Sequence) \
                or not folders or len(folders) > 50:
            raise CaptureError("scope_folders_invalid")
        roots: list[str] = []
        for folder in folders:
            if not isinstance(folder, str) or not (
                    folder == "/" or grammar.TYPES["drive_path"].valid(folder)):
                raise CaptureError("scope_folders_invalid")
            if folder in roots:
                raise CaptureError("scope_folders_invalid")
            roots.append(folder)
        if any(a != b and _under(b, a) for a in roots for b in roots):
            raise CaptureError("scope_folders_nested")
        self.folders = tuple(roots)
        self.max_depth = self._bound(entry.get("max_depth", 3), 0, 20)
        self.max_folders = self._bound(entry.get("max_folders", 200), 1, 5000)

    @staticmethod
    def _bound(value: Any, low: int, high: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise CaptureError("scope_bound_invalid")
        return value


def _library_name_ok(value: Any) -> bool:
    return (isinstance(value, str) and 0 < len(value) <= 128 and value == value.strip()
            and not any(ord(c) < 32 or ord(c) == 127 for c in value))


class DiscoveryRule:
    """The estate discovery of a scope (``discover``): every document library of every
    site the application may read on the configured host, each walked from ``/`` with
    the given bounds, except what ``exclude`` names. An exclusion is data: ``site`` (a
    site URL name: the whole site, its libraries never listed), ``library`` (a library
    name on every site) or both (that library of that site), matched case-insensitively."""

    def __init__(self, entry: Mapping[str, Any]) -> None:
        allowed = {"max_sites", "max_libraries_per_site", "max_depth", "max_folders",
                   "exclude", "note"}
        if not isinstance(entry, Mapping) or set(entry) - allowed:
            raise CaptureError("scope_discover_invalid")
        bound = CaptureTarget._bound
        self.max_sites = bound(entry.get("max_sites", 500), 1, 5000)
        self.max_libraries = bound(entry.get("max_libraries_per_site", 50), 1, 500)
        self.max_depth = bound(entry.get("max_depth", 8), 0, 20)
        self.max_folders = bound(entry.get("max_folders", 5000), 1, 5000)
        rules = entry.get("exclude", [])
        if isinstance(rules, (str, bytes)) or not isinstance(rules, Sequence) or len(rules) > 200:
            raise CaptureError("scope_exclude_invalid")
        self.exclude: list[tuple[str | None, str | None]] = []
        for rule in rules:
            if (not isinstance(rule, Mapping) or set(rule) - {"site", "library", "note"}
                    or not ({"site", "library"} & set(rule))
                    or ("site" in rule and not grammar.TYPES["site_name"].valid(rule["site"]))
                    or ("library" in rule and not _library_name_ok(rule["library"]))):
                raise CaptureError("scope_exclude_invalid")
            self.exclude.append((rule["site"].casefold() if "site" in rule else None,
                                 rule["library"].casefold() if "library" in rule else None))

    def site_excluded(self, name: str | None) -> bool:
        site = (name or "").casefold()
        return any(s == site and lib is None for s, lib in self.exclude)

    def library_excluded(self, site_name: str | None, library: str | None) -> bool:
        site, name = (site_name or "").casefold(), (library or "").casefold()
        return any(lib == name and s in (None, site) for s, lib in self.exclude
                   if lib is not None)


class LibraryClass:
    """One ``library_security_classes`` entry: the security class of the library a
    ``drive_id`` names, or of the library called ``library`` (on ``site``, else on
    every site), matched case-insensitively."""

    def __init__(self, entry: Mapping[str, Any]) -> None:
        if (not isinstance(entry, Mapping)
                or set(entry) - {"drive_id", "site", "library", "class", "note"}
                or ("drive_id" in entry) == ("library" in entry)
                or ("site" in entry and "library" not in entry)
                or entry.get("class") not in taxonomy.SECURITY_CLASSES
                or ("drive_id" in entry and not grammar.TYPES["drive_id"].valid(entry["drive_id"]))
                or ("site" in entry and not grammar.TYPES["site_name"].valid(entry["site"]))
                or ("library" in entry and not _library_name_ok(entry["library"]))):
            raise CaptureError("scope_library_class_invalid")
        self.drive_id = entry.get("drive_id")
        self.site = entry["site"].casefold() if "site" in entry else None
        self.library = entry["library"].casefold() if "library" in entry else None
        self.security_class = entry["class"]


class CaptureScope:
    """Which drives/sites and folders are captured, how deep and how large, plus the
    optional estate discovery (:class:`DiscoveryRule`), the classes of its libraries
    (:class:`LibraryClass`) and the SharePoint columns kept per version. Configuration,
    never a grant: the capture actor still needs the live-source audience."""

    def __init__(self, targets: Sequence[Mapping[str, Any]], *,
                 max_content_bytes: int = MAX_CONTENT_BYTES,
                 full_walk_hours: int = FULL_WALK_HOURS,
                 permission_reread_hours: int = PERMISSION_REREAD_HOURS,
                 census_max_age_hours: int | None = None,
                 discover: Mapping[str, Any] | None = None,
                 library_security_classes: Sequence[Mapping[str, Any]] = (),
                 list_item_columns: Sequence[str] = ()) -> None:
        self.discover = None if discover is None else DiscoveryRule(discover)
        if isinstance(library_security_classes, (str, bytes)) or not isinstance(
                library_security_classes, Sequence) or len(library_security_classes) > 200:
            raise CaptureError("scope_library_class_invalid")
        self.library_classes = tuple(LibraryClass(e) for e in library_security_classes)
        if list_item_columns == taxonomy.CAPTURE_COLUMNS:
            list_item_columns = tuple(column["name"] for column in taxonomy.COLUMNS)
        if (isinstance(list_item_columns, (str, bytes))
                or not isinstance(list_item_columns, Sequence) or len(list_item_columns) > 20
                or not all(grammar.column_name_ok(c) for c in list_item_columns)
                or len(set(list_item_columns)) != len(list_item_columns)):
            raise CaptureError("scope_list_item_columns_invalid")
        if not set(list_item_columns) <= set(taxonomy.COLUMN_BY_NAME):
            # The Portal's column set is consumed, never forked (QA-06).
            raise CaptureError("scope_list_item_column_not_in_taxonomy")
        self.list_item_columns = tuple(list_item_columns)
        if isinstance(targets, (str, bytes)) or not isinstance(targets, Sequence) or (
                not targets and self.discover is None):
            raise CaptureError("scope_empty")
        self.targets = tuple(CaptureTarget(t) for t in targets)
        if len({t.id for t in self.targets}) != len(self.targets):
            raise CaptureError("scope_target_duplicate")
        if isinstance(max_content_bytes, bool) or not isinstance(max_content_bytes, int) \
                or not 1 <= max_content_bytes <= MAX_DECLARED_CONTENT_BYTES:
            raise CaptureError("scope_max_content_bytes_invalid")
        self.max_content_bytes = max_content_bytes
        self.full_walk_hours = CaptureTarget._bound(full_walk_hours, 1, 720)
        self.permission_reread_hours = CaptureTarget._bound(permission_reread_hours, 1, 720)
        # How long the host's coverage census may be carried across calls (its own window,
        # for a check that runs less often than the full walk); default full_walk_hours.
        self.census_max_age_hours = (
            self.full_walk_hours if census_max_age_hours is None
            else CaptureTarget._bound(census_max_age_hours, 1, 720))

    def library_class(self, drive_id: str, site: str | None, library: str | None
                      ) -> str | None:
        """The class the scope gives this library: by drive id, else by name on its
        site, else by name on every site; None when it gives none."""
        site_key, name = (site or "").casefold(), (library or "").casefold()
        for match in (lambda e: e.drive_id == drive_id,
                      lambda e: library is not None and e.library == name
                      and e.site is not None and e.site == site_key,
                      lambda e: library is not None and e.library == name and e.site is None):
            found = {e.security_class for e in self.library_classes if match(e)}
            if found:
                # Two classes for one library: nobody may read it.
                return found.pop() if len(found) == 1 else taxonomy.UNRESOLVED_CLASS
        return None

    @classmethod
    def from_json(cls, document: Any) -> "CaptureScope":
        allowed = {"schema", "note", "targets", "max_content_bytes", "full_walk_hours",
                   "permission_reread_hours", "census_max_age_hours", "discover",
                   "library_security_classes", "list_item_columns"}
        if (not isinstance(document, dict) or document.get("schema") != SCOPE_SCHEMA
                or set(document) - allowed or not isinstance(document.get("targets"), list)):
            raise CaptureError("scope_document_invalid")
        return cls(document["targets"],
                   max_content_bytes=document.get("max_content_bytes", MAX_CONTENT_BYTES),
                   full_walk_hours=document.get("full_walk_hours", FULL_WALK_HOURS),
                   permission_reread_hours=document.get("permission_reread_hours",
                                                        PERMISSION_REREAD_HOURS),
                   census_max_age_hours=document.get("census_max_age_hours"),
                   discover=document.get("discover"),
                   library_security_classes=document.get("library_security_classes", []),
                   list_item_columns=document.get("list_item_columns", []))

    @classmethod
    def from_file(cls, path: str | Path) -> "CaptureScope":
        try:
            document = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise CaptureError("scope_unreadable") from None
        return cls.from_json(document)
