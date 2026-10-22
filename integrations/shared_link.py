"""Guarded read of one SharePoint/OneDrive sharing URL to its original (GET only).

``read_shared_link`` is the one entry point: a sharing URL (or an item's own
``webUrl``) on the tenant's SharePoint or OneDrive host (``GuardedTransport.
download_hosts``; anything else, or no configured host, is refused before I/O)
is encoded as Graph documents it (``u!`` + unpadded base64url,
``live_source_grammar.encode_share_id``) and resolved by one metadata GET,
``/v1.0/shares/{share}/driveItem?$select=id,parentReference`` (identity only).
The item is then read like any drive item: ``SharePointReader.get_content`` on
its drive (canonical path from server metadata, audience and source-ACL
decision, then ``/drives/{drive}/items/{item}/content`` with its 302 fetched
credential-free, eTag and path re-read, size and source-hash checks). An actor
with no SharePoint grant causes no I/O; a refused, withheld or unresolvable
link reads ``not_visible``. Every request passes ``live_source_guard.
check_request`` (closed grammar: no other ``/shares/`` path, GET only).

The result is the original bytes with ``drive_citation``
(``sharepoint://<drive>/<item>@<eTag>#sha256=<hex of the bytes read>``). The
sharing URL itself is never returned or printed: an "anyone" link is a bearer
credential.

Probe (``python3 -m integrations.shared_link probe``; counts and booleans only):
resolves the ``webUrl`` of one root file (<= 1 MB) of ``SHAREPOINT_DRIVE_ID``
through ``/shares/`` and compares item and sha256 with the drive's own
download-URL read. Live-unverified until
``verify/live-checks/graph.shared_link_read.json`` passes.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

from ontology.core import Actor

from . import live_source_grammar as grammar
from .graph_read import SharePointReader, build_graph
from .live_source_guard import (GuardedTransport, LiveSourceAudience, NotVisible,
                                ReadOnlyGuardError, SourceApi, SourceNotFound, SourceUnavailable)

MAX_SHARING_URL = 2048
_SHARE_SELECT = "id,parentReference"


def _check_sharing_url(sharing_url: Any, transport: GuardedTransport) -> str:
    hosts = transport.download_hosts
    if not hosts:
        raise ReadOnlyGuardError("no SharePoint host configured; shared links refused")
    if (not isinstance(sharing_url, str) or len(sharing_url) > MAX_SHARING_URL
            or any(ord(c) <= 32 or ord(c) == 127 for c in sharing_url)):
        raise NotVisible()
    try:
        parts = urlsplit(sharing_url)
        host, port = (parts.hostname or "").lower(), parts.port
    except ValueError:
        raise NotVisible() from None
    if (parts.scheme != "https" or parts.username or parts.password or port is not None
            or parts.fragment or host not in hosts or parts.netloc.lower() != host):
        raise NotVisible()
    return grammar.encode_share_id(sharing_url)


def resolve_shared_link(api: SourceApi, transport: GuardedTransport,
                        audience: LiveSourceAudience, actor: Actor,
                        sharing_url: str) -> tuple[str, str]:
    """``(drive_id, item_id)`` of the item (file or folder) a sharing URL names: the one
    ``/shares/`` metadata GET, identity only. The item itself is not opened here: its
    audience and ACL are decided by the ``SharePointReader`` that reads it."""
    share = _check_sharing_url(sharing_url, transport)
    if not audience.may_read_any(actor, "sharepoint:"):
        raise NotVisible()
    try:
        raw = api.get_json(grammar.url("graph.share_drive_item", {"$select": _SHARE_SELECT},
                                       share=share))
    except SourceNotFound:
        raise NotVisible() from None
    parent = raw.get("parentReference") if isinstance(raw, Mapping) else None
    drive_id = parent.get("driveId") if isinstance(parent, Mapping) else None
    item_id = raw.get("id") if isinstance(raw, Mapping) else None
    if not grammar.TYPES["drive_id"].valid(drive_id) or not grammar.TYPES["item_id"].valid(item_id):
        raise SourceUnavailable("shared link resolved to no drive item identity")
    return drive_id, item_id


def read_shared_link(api: SourceApi, transport: GuardedTransport, audience: LiveSourceAudience,
                     actor: Actor, sharing_url: str, *, max_bytes: int = 20_000_000,
                     read_permissions: bool = False) -> dict[str, Any]:
    """The original a sharing URL names, for ``actor`` (see module docstring).

    Returns ``drive_id``, ``item_id``, ``etag``, ``content_sha256`` (of the bytes
    read; None when the file exceeds ``max_bytes``), ``drive_citation``,
    ``content_status`` (``read`` / ``exceeds_cap``), ``content`` and ``record``."""
    drive_id, item_id = resolve_shared_link(api, transport, audience, actor, sharing_url)
    reader = SharePointReader(api, audience, drive_id, transport=transport,
                              read_permissions=read_permissions)
    read = reader.get_content(actor, item_id, max_bytes=max_bytes, content_route=True)
    record = read["record"]
    return {"source": "sharepoint-shared-link", "drive_id": drive_id, "item_id": item_id,
            "etag": record["etag"], "content_sha256": read.get("content_sha256"),
            "drive_citation": record["citation"], "content_status": read["content_status"],
            "content": read["content"], "record": record}


def probe(environ: Mapping[str, str] | None = None, *, sender=None,
          max_content_bytes: int = 1_000_000) -> dict[str, Any]:
    """Counts and booleans only; never prints a URL, name or id."""
    env = os.environ if environ is None else environ
    api, transport = build_graph(env, sender=sender)
    drive_id = env.get("SHAREPOINT_DRIVE_ID")
    report: dict[str, Any] = {"source": "microsoft-graph", "evidence_label": "live-read",
                              "read_only_guard": "GET-only; token POST only",
                              "download_host_pinned": bool(transport.download_hosts)}
    if not grammar.TYPES["drive_id"].valid(drive_id):
        raise ValueError("SHAREPOINT_DRIVE_ID invalid")
    audience = LiveSourceAudience({"sharepoint:*": ["probe:operator"]}, tenant="probe")
    actor = Actor("probe", frozenset({"probe:operator"}), "restricted")
    drive = SharePointReader(api, audience, drive_id, transport=transport)
    files = [i for i in drive.list_folder(actor, "/", max_pages=2, max_items=200)["items"]
             if i["kind"] == "file" and isinstance(i.get("size"), int)
             and i["size"] <= max_content_bytes and isinstance(i.get("original_url"), str)]
    report["token"] = "ok"
    if not files:
        report["status"] = "no_root_file_under_cap"
        return report
    item = files[0]
    shared = read_shared_link(api, transport, audience, actor, item["original_url"],
                              max_bytes=max_content_bytes)
    direct = drive.get_content(actor, item["id"], max_bytes=max_content_bytes)
    report.update({
        "status": shared["content_status"], "bytes": len(shared["content"] or b""),
        "same_item": (shared["drive_id"], shared["item_id"]) == (drive_id, item["id"]),
        "sha256_cited": f"#sha256={shared['content_sha256']}" in shared["drive_citation"],
        "sha256_matches_download_url_read": shared["content_sha256"]
        == direct.get("content_sha256"),
        "requests_sent": transport.requests_sent,
        "operations_read": sorted(transport.operations_read)})
    return report


def main(argv: Sequence[str] | None = None, *, environ: Mapping[str, str] | None = None,
         sender=None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m integrations.shared_link",
                                     description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("probe", help="resolve one root file's webUrl through /shares/ and "
                                      "compare with the drive read; counts and booleans only")
    parser.parse_args(argv)
    try:
        result = probe(environ, sender=sender)
    except ReadOnlyGuardError as exc:
        print(json.dumps({"error": "read_only_guard", "message": str(exc)}), file=sys.stderr)
        return 2
    except (SourceUnavailable, NotVisible, ValueError) as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=1, sort_keys=True))
    return 0


__all__ = ["MAX_SHARING_URL", "main", "probe", "read_shared_link", "resolve_shared_link"]


if __name__ == "__main__":
    raise SystemExit(main())
