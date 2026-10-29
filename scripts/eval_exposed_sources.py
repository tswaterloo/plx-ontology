"""Do the originals the exposed eval items cite still exist, unchanged? (GET only)

The exposed real-case items (``evals/exposed/dev-*.json``) cite originals by identity:
SharePoint drive items (drive id, item id, eTag, SHA-256 or quickXorHash), members of a
pinned archive (member path, SHA-256) and mail messages with their attachments (message
id, received time, attachment name, SHA-256). Those identities live in
``evals/exposed/sources.json`` (evaluation data, never product code). This module reads
them only through the existing GET-only Graph readers (``operating.case_runtime.
build_live_readers``: ``SharePointReader`` and ``GraphMailReader``) with a probe audience,
and compares:

* ``documents``: every drive item's eTag, its byte SHA-256 when pinned (``sha256_basis``
  ``bytes``; ``utf8_text`` also accepts the SHA-256 of the UTF-8 text without a BOM) and
  else its quickXorHash from the item metadata (no download); an archive whose SHA-256
  matches is opened in memory and each pinned member's SHA-256 is compared.
* ``mail``: the mailbox comes from ``PLX_EVAL_EXPOSED_MAILBOX`` (the sources record no
  mailbox). The pinned ids are REST ids while the reader works in immutable ids, so a
  message is located by its exact received time (one listing GET, oldest first, from that
  second) and an attachment by its name, then its bytes are read and their SHA-256
  compared. ``id_matches_pinned`` reports whether the located id equals the pinned one
  (informational: an immutable id never does).

Output: counts, booleans, row indexes (the order of ``sources.json``) and error classes
only; never a name, path, id, URL or text. Exit 0 only when every pinned original is
found and matches.

    python3 -m scripts.eval_exposed_sources documents [--sources PATH]
    python3 -m scripts.eval_exposed_sources mail [--sources PATH]

Evidence label: live-unverified until the live verifier runs
live check ``case.exposed_originals`` / ``case.exposed_mail.json``; tests
use an injected transport (``tests/test_eval_exposed_cases.py``).
"""

from __future__ import annotations

import argparse
import datetime as dt
from hashlib import sha256
import io
import json
import os
from pathlib import Path
import re
import sys
import zipfile
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
SOURCES_PATH = ROOT / "evals" / "exposed" / "sources.json"
SOURCES_SCHEMA = "plx-eval-exposed-sources/v1"
MAILBOX_ENV = "PLX_EVAL_EXPOSED_MAILBOX"
PROBE_PRINCIPAL = "probe:eval-sources"
MAX_BYTES = 40_000_000          # the largest pinned original is a ~31 MB archive
MAX_MEMBER_BYTES = 20_000_000
LISTING_PAGE = 10
READ_ONLY_GUARD = "GET-only; token POST only"
_HEX64 = re.compile(r"[0-9a-f]{64}")
_BASES = ("bytes", "utf8_text")


def load_sources(path: str | Path = SOURCES_PATH) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("schema") != SOURCES_SCHEMA:
        raise ValueError("exposed_sources_schema")
    for item in data.get("drive_items") or []:
        pinned = item.get("sha256")
        if not all(isinstance(item.get(key), str) and item[key]
                   for key in ("source_id", "drive_id", "item_id", "e_tag")) or \
                (pinned is not None and (not _HEX64.fullmatch(str(pinned))
                                         or item.get("sha256_basis") not in _BASES)) or \
                (pinned is None and not item.get("quick_xor_hash")):
            raise ValueError("exposed_sources_drive_item_invalid")
    for entry in [*(data.get("archive_members") or []), *(data.get("mail_attachments") or [])]:
        if not _HEX64.fullmatch(str(entry.get("sha256"))):
            raise ValueError("exposed_sources_sha256_invalid")
    return data


def _actor():
    from ontology.core import Actor
    return Actor("probe", frozenset({PROBE_PRINCIPAL}), "restricted")


def _sha_matches(content: bytes, pinned: str, basis: str) -> bool:
    if sha256(content).hexdigest() == pinned:
        return True
    if basis != "utf8_text":
        return False
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        return False
    return sha256(text.encode("utf-8")).hexdigest() == pinned


def _members_matching(content: bytes, members: Sequence[Mapping[str, Any]]) -> int:
    """Pinned members whose SHA-256 matches, read from an archive already matched by hash."""
    matched = 0
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        infos = {info.filename: info for info in archive.infolist()}
        for member in members:
            info = infos.get(member["member"])
            if info is None or info.file_size > MAX_MEMBER_BYTES:
                continue
            matched += sha256(archive.read(info)).hexdigest() == member["sha256"]
    return matched


def _document_row(reader: Any, actor: Any, item: Mapping[str, Any],
                  members: Sequence[Mapping[str, Any]], max_bytes: int) -> dict[str, Any]:
    from integrations.live_source_guard import NotVisible, SourceUnavailable, failure_diagnostics

    pinned = item.get("sha256")
    row: dict[str, Any] = {"status": "not_configured", "etag_current": None,
                           "sha256_pinned": pinned is not None, "sha256_match": None,
                           "quick_xor_match": None, "members_pinned": len(members),
                           "members_match": 0, "verified": False}
    if reader is None:
        return row
    try:
        if pinned is not None:
            read = reader.get_content(actor, item["item_id"], max_bytes=max_bytes)
        else:
            read = {"record": reader.get_item(actor, item["item_id"]), "content": None,
                    "content_status": "read"}
    except (SourceUnavailable, NotVisible) as error:
        row.update(status="error", **failure_diagnostics(error))
        return row
    record = read["record"]
    row["status"] = read.get("content_status") or "read"
    row["etag_current"] = record.get("etag") == item["e_tag"]
    listed = (record.get("source_hashes") or {}).get("quickXorHash")
    if item.get("quick_xor_hash") and isinstance(listed, str):
        row["quick_xor_match"] = listed == item["quick_xor_hash"]
    content = read.get("content")
    if pinned is not None:
        if content is None:
            return row  # exceeds_cap: not verified
        row["sha256_match"] = _sha_matches(content, pinned, item["sha256_basis"])
        if members and row["sha256_match"]:
            row["members_match"] = _members_matching(content, members)
        row["verified"] = row["sha256_match"]
    else:
        row["verified"] = row["quick_xor_match"] is True
    return row


def documents(environ: Mapping[str, str] | None = None, *, sender: Any = None,
              sources_path: str | Path = SOURCES_PATH,
              max_bytes: int = MAX_BYTES) -> dict[str, Any]:
    """Every pinned drive item (and archive member) read through the GET-only reader."""
    from integrations.live_source_guard import LiveSourceAudience
    from operating.case_runtime import build_live_readers

    env = os.environ if environ is None else environ
    sources = load_sources(sources_path)
    items = sources["drive_items"]
    drives = sorted({item["drive_id"] for item in items})
    audience = LiveSourceAudience({f"sharepoint:{drive}:/*": [PROBE_PRINCIPAL]
                                   for drive in drives}, tenant="probe")
    readers = build_live_readers(audience, env, sharepoint_drives=drives, graph_sender=sender)
    actor = _actor()
    members: dict[str, list[Mapping[str, Any]]] = {}
    for member in sources.get("archive_members") or []:
        members.setdefault(member["archive"], []).append(member)
    rows = []
    for index, item in enumerate(items):
        row = _document_row(readers.sharepoint.get(item["drive_id"]), actor, item,
                            members.get(item["source_id"], []), max_bytes)
        rows.append({"index": index, **row})
    members_pinned = sum(len(group) for group in members.values())
    report: dict[str, Any] = {
        "check": "case.exposed_originals", "source": "sharepoint-drive",
        "evidence_label": "live-read", "read_only_guard": READ_ONLY_GUARD,
        "drives": len(drives),
        "drives_configured": sum(1 for drive in drives if drive in readers.sharepoint),
        "items": len(items), "rows": rows,
        "read": sum(1 for row in rows if row["status"] == "read"),
        "read_errors": sum(1 for row in rows if row["status"] == "error"),
        "verified": sum(1 for row in rows if row["verified"]),
        "sha256_pinned": sum(1 for row in rows if row["sha256_pinned"]),
        "sha256_match": sum(1 for row in rows if row["sha256_match"]),
        "quick_xor_only": sum(1 for row in rows if not row["sha256_pinned"]),
        "quick_xor_match": sum(1 for row in rows if row["quick_xor_match"]),
        "etag_current": sum(1 for row in rows if row["etag_current"]),
        "members_pinned": members_pinned,
        "members_match": sum(row["members_match"] for row in rows),
        "requests_sent": getattr(readers.graph_transport, "requests_sent", None)}
    report["configured"] = report["drives_configured"] == len(drives)
    report["ok"] = (report["verified"] == len(items)
                    and report["members_match"] == members_pinned)
    return report


def _instant(value: Any) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(
            microsecond=0)
    except ValueError:
        return None


def _mail_row(reader: Any, actor: Any, mailbox: str, message: Mapping[str, Any],
              attachments: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    from integrations.live_source_guard import NotVisible, SourceUnavailable, failure_diagnostics

    row: dict[str, Any] = {"status": "not_found", "id_matches_pinned": None,
                           "attachments_pinned": len(attachments), "attachments_match": 0,
                           "attachments": []}
    try:
        listing = reader.list_messages(actor, mailbox, received_since=message["received_at"],
                                       page_size=LISTING_PAGE, max_pages=1,
                                       max_items=LISTING_PAGE, oldest_first=True)
        when = _instant(message["received_at"])
        found = [item for item in listing["items"] if _instant(item.get("received_at")) == when]
        if len(found) != 1:
            row["status"] = "not_found" if not found else "ambiguous"
            return row
        located = found[0]
        row.update(status="located", id_matches_pinned=located["id"] == message["message_id"])
        listed = reader.list_attachments(actor, mailbox, located["id"])["items"] \
            if attachments else []
    except (SourceUnavailable, NotVisible) as error:
        row.update(status="error", **failure_diagnostics(error))
        return row
    for attachment in attachments:
        named = [item for item in listed if item.get("name") == attachment["name"]]
        entry: dict[str, Any] = {"status": "not_found" if not named else "ambiguous",
                                 "sha256_match": None, "bytes_match": None}
        if len(named) == 1:
            try:
                read = reader.get_attachment(actor, mailbox, located["id"], named[0]["id"])
            except (SourceUnavailable, NotVisible) as error:
                entry.update(status="error", **failure_diagnostics(error))
            else:
                entry.update(status="read",
                             sha256_match=read["attachment"]["content_sha256"] ==
                             attachment["sha256"],
                             bytes_match=read["attachment"]["bytes"] == attachment["bytes"])
        row["attachments"].append(entry)
    row["attachments_match"] = sum(1 for entry in row["attachments"] if entry["sha256_match"])
    return row


def mail(environ: Mapping[str, str] | None = None, *, sender: Any = None,
         sources_path: str | Path = SOURCES_PATH) -> dict[str, Any]:
    """Every pinned message located in the configured mailbox, its pinned attachments read."""
    from integrations.live_source_guard import LiveSourceAudience
    from operating.case_runtime import build_live_readers

    env = os.environ if environ is None else environ
    sources = load_sources(sources_path)
    messages = sources.get("mail_messages") or []
    attachments: dict[str, list[Mapping[str, Any]]] = {}
    for attachment in sources.get("mail_attachments") or []:
        attachments.setdefault(attachment["message"], []).append(attachment)
    report: dict[str, Any] = {"check": "case.exposed_mail", "source": "graph-mail",
                              "evidence_label": "live-read", "read_only_guard": READ_ONLY_GUARD,
                              "messages": len(messages), "configured": False, "ok": False}
    mailbox = (env.get(MAILBOX_ENV) or "").strip()
    if not mailbox:
        report["error"] = "mailbox_not_configured"
        return report
    audience = LiveSourceAudience({"mail:*": [PROBE_PRINCIPAL]}, tenant="probe")
    readers = build_live_readers(audience, env, graph_sender=sender)
    if readers.mail is None:
        report["error"] = "source_not_configured"
        return report
    actor = _actor()
    rows = []
    with readers.mail.directory_cache():
        for index, message in enumerate(messages):
            row = _mail_row(readers.mail, actor, mailbox, message,
                            attachments.get(message["source_id"], []))
            rows.append({"index": index, **row})
    pinned = sum(len(group) for group in attachments.values())
    report.update(
        configured=True, rows=rows,
        located=sum(1 for row in rows if row["status"] == "located"),
        read_errors=sum(1 for row in rows if row["status"] == "error"),
        id_matches_pinned=sum(1 for row in rows if row["id_matches_pinned"]),
        attachments_pinned=pinned,
        attachments_match=sum(row["attachments_match"] for row in rows),
        requests_sent=getattr(readers.graph_transport, "requests_sent", None))
    report["ok"] = report["located"] == len(messages) and report["attachments_match"] == pinned
    return report


def main(argv: Sequence[str] | None = None, *, environ: Mapping[str, str] | None = None,
         sender: Any = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m scripts.eval_exposed_sources",
                                     description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=("documents", "mail"))
    parser.add_argument("--sources", type=Path, default=SOURCES_PATH)
    args = parser.parse_args(argv)
    from integrations.live_source_guard import (NotVisible, ReadOnlyGuardError,
                                                SourceUnavailable, failure_diagnostics)
    run = documents if args.command == "documents" else mail
    try:
        report = run(environ, sender=sender, sources_path=args.sources)
    except ReadOnlyGuardError:
        print(json.dumps({"error": "read_only_guard", "rule": "get_only"}), file=sys.stderr)
        return 2
    except (SourceUnavailable, NotVisible, ValueError) as error:
        # The live verifier publishes error, http_status and error_code from this line.
        diagnosis = {key: value for key, value in failure_diagnostics(error).items()
                     if value is not None and key != "error_class"}
        print(json.dumps({"error": type(error).__name__, **diagnosis}), file=sys.stderr)
        return 1
    print(json.dumps(report, indent=1, sort_keys=True))
    return 0 if report.get("ok") else 1


__all__ = ["MAILBOX_ENV", "SOURCES_PATH", "documents", "load_sources", "mail", "main"]


if __name__ == "__main__":
    raise SystemExit(main())
