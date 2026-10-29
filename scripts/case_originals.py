"""Stakeholder case originals: a pinned registry and a full read of every registered file.

``registry`` turns the immutable source-pin records (``plx-advisory/v1`` context
records) into the committed, nonsecret registry ``examples/case_originals.json``
(``plx-case-originals/v1``). Inputs: the baseline record (``source_inventory``: the
submitted originals, archive members, linked business files and mail attachments, plus
its gaps) and, optionally, an incremental record that adds to it without overwriting it
(``delta_source_pins``: newly downloaded originals, source aliases of baseline bytes,
derived renderings; ``case_register``; ``historical_receiving_source_pins``;
``open_gaps``, which supersede baseline gaps with the same item). One row per file record
with a stable id, owner, case (``case_register`` id; ``case_ids`` lists every case a row
belongs to, inherited from its container and the map that links it), kind
(``original``, ``derivative``, ``archive_member``, ``attachment`` or ``mail_message``;
members and attachments name their parent row, an alias row the row whose bytes it
names), the canonical source (sharing URL, Teams message reference, drive and item),
the provider revision (version id, eTag) where recorded, expected bytes and SHA-256,
provider MIME and detected media type, exposure ``dev-exposed`` (read by builders,
never held out). A pin a record names but whose bytes it did not fetch (a historical
revision, a historical mail pin) is its own ``unfetched`` row, never merged into the
current one; each gap is a ``gap`` row (``superseded`` when a later record restates it).
The header records every record commit, record SHA-256 and ``source_revision``. No
file body, temporary download URL, local path or secret is in it. ``watch_folders``
(given to the generator) are the folders the reader lists so that new files there are
discovered, not only the pinned ones. ``--capture CASE GAP COMMIT PATH FILE`` adds one
``capture`` row per advisory capture record (``plx-advisory/v1``, a ``capture`` with the
provider ``original``: drive, item, name, size, quickXorHash; no bytes) bound to a case and
its open gap: ``oversize`` (with ``oversize``: bytes and the bound
``use_case_intake.MAX_CONTENT_BYTES``) when the provider size is over the reader's content
bound, else ``unpinned``. An unpinned capture is pinned (SHA-256, bytes and member
manifest, with one ``archive_member`` row per member, a member over ``MAX_PART_BYTES``
``oversize``) only from a verified receipt of a full read (``--pin-receipts DIR``), never
from the record. An unknown or malformed capture input fails closed.
``--manifest CASE COMMIT PATH FILE`` adds the rows of an advisory originals manifest
(``source_manifest``, ``nested_attachment_manifest``, ``zip_member_manifest``; every
section and entry key checked exactly, counts checked against the record's scope) bound to
a case: a source row is routed by drive id and path (the drive derived from its list and
site, the URL not kept), a nested attachment names its ``.msg`` parent, a zip member its
zip attachment and the rows whose bytes it repeats; a payload seen before is an alias of
its first row (``same_bytes_as``) and counted once (``payloads``). ``--caveats FILE``
attaches a caveats file's entries verbatim to the source record they qualify.
``--crosswalk REPO_PATH`` adds ``crosswalk``: one row per watched source and descriptor
of an eval case's proposed items, naming the registry row it maps to (by item id or
SHA-256) or why it is unmapped; no body, URL or secret. ``--card-sources REPO_PATH``
(``plx-case-originals-card-sources/v1``, ``examples/case_originals_card_sources.json``) adds
the originals an eval case card names that no source-pin record lists, as rows of origin
``card``: a named ``gap``, or a ``chat_attachment`` (a file shared in a Teams chat: its
content URL, the chat message link and attachment id) pinned only when the advisory
record it cites, read from the local Git objects, names the same URL, SHA-256 and chat
message; ``read`` fetches it through that URL like any shared file and the host serves it
behind the chat message's gate; or a ``rendered_artifact`` (a claude.ai artifact held as
its complete rendered text in an source record committed to a repository), a pinned
derivative only when that record's ``source_revision``, ``source_text`` SHA-256 and size
and its stated rendered character count all agree, read from the record at its commit
(``repository_ref``) and served to readers granted that repository. An unknown or malformed manifest, caveat, crosswalk or
card-sources input fails closed.

``read`` fetches every registered original through the GET-only Graph clients only: a
row with a drive item through ``SharePointReader.get_content``, a manifest row by its
drive path (its parent folder listed once, the exact name, its eTag GUID compared with the
list item unique id), a row with only a
sharing/OneDrive URL through ``integrations.shared_link.read_shared_link``, a derivative
held in this repository's history (``repository_ref``: a GitHub blob URL at a commit) from
the local Git object store only (``git cat-file``, no fetch). An unfetched historical
revision reads its named version (``SharePointReader.get_version_content``, matched by
size when the pin names no SHA-256); a historical mail pin is located in the mailbox
``PLX_EVAL_EXPOSED_MAILBOX`` names by its exact received second (``GraphMailReader``),
its attachments by name. Other unfetched rows, and mail pins with no mailbox configured,
read ``no_route`` with their reason. The bytes are checked against the
registry (size and SHA-256; a capture: size and the provider quickXorHash), their type
is detected from the bytes (``operating.use_case_intake.detect_type``: a PNG named
``.pdf`` is an image and is OCRed; the original name and provider MIME stay as
registered) and extracted by the one part reader (``operating.use_case_parts.account``).
Archive members and mail attachments are verified through their container's one read
(``use_case_parts.members``), each with the extraction of its own parts. A row proven
by an earlier recorded capture (``proven_by``, kept on its receipt) is stored and
extracted like any other read: the host serves originals from this store only. A pinned file whose current bytes differ reads ``mismatch``; the pinned row is
kept and the served bytes are their own revision receipt (``rv-`` id, ``revision_of``
the row, read and extracted).

Capture store (``--state-dir``, default ``PLX_WORKBENCH_STATE_DIR``, the live verifier's
store; never inside Git): original bytes at ``<state>/case-originals/<sha256>``,
extracted text at ``<state>/case-originals/extractions/<sha256>.txt`` and one receipt per
file at ``<state>/case-originals/receipts/<registry sha256>/<entry id>.json`` (a
discovered file's at ``receipts/discovered/<entry id>.json``, kept across registry versions)
(``plx-case-original-receipt/v1``): registry version, entry, canonical source, provider
revision, bytes, SHA-256, match and its basis, body status (``BODY_STATUSES``; 401/403
``denied``, 404 ``gone``, any other failure ``unavailable``, each with the exact HTTP
status and Graph error code), member manifest, extraction status and methods, read
time and the reader identity (``CLAUDE_CODE_ACCOUNT_UUID``,
``CLAUDE_CODE_REMOTE_SESSION_ID``, ``CLAUDE_CODE_CONTAINER_ID``, run id, role,
environment). Every file in a watch folder that the registry does not list is read in
full like an original (``discovered_read``, its own receipt; a person adds it to the
registry); an item with no file facet is ``discovered_not_file``, a failure names its HTTP
status and Graph error code. One listed with the eTag and size an earlier run (of any
registry version) read in full, whose stored bytes re-hash, is not read again: its receipt is
reused and says so (``reused``; ``discovered_reused``; ``--reread-unchanged`` reads it
anyway), so a watch folder larger than one budget is read across runs. stdout is the live-check report: counts by status,
``failure_causes`` and ``problem_pages`` (entry id -> one enum token), ``captures`` and
``entry_pages`` (per entry: status, name, watch folder, case, kind, source, SHA-256
match, HTTP status, error code, detail, extraction, revision). Never a body, extracted
text or URL; the live check records ids, enums and counts only, never ``entry_pages``.

    python3 -m scripts.case_originals registry --from BASELINE.json --advisory-commit SHA \\
        --advisory-path PATH [--delta DELTA.json --delta-commit SHA --delta-path PATH] \\
        [--proven CHECK_ID=FILE ...] [--watch-link URL ...] \\
        [--watch-drive DRIVE_ID PATH WEB_URL ...] \\
        [--capture CASE GAP COMMIT PATH FILE ...] [--pin-receipts DIR] \\
        [--out examples/case_originals.json]
    python3 -m scripts.case_originals read --run-id ID --role verifier|worker|supervisor \\
        [--environment NAME] [--registry examples/case_originals.json] [--state-dir DIR] \\
        [--no-watch] [--budget-seconds N] [--reread-unchanged]

Evidence label: live-unverified until live check ``case.originals_read`` has
a verifier result; tests use an injected transport (``tests/test_case_originals.py``).
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import time
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = ROOT / "examples" / "case_originals.json"
REPOSITORY_ROOT = ROOT  # the Git object store a repository_ref is read from (local only)
SCHEMA = "plx-case-originals/v1"
RECEIPT_SCHEMA = "plx-case-original-receipt/v1"
REPORT_CHECK = "case.originals_read"
ADVISORY_SCHEMA = "plx-advisory/v1"
EXPOSURE = "dev-exposed"
STORE_DIR = "case-originals"
# discovered (``nr-``) receipts live outside the registry-version directory so a registry
# change never discards an earlier read; ``_Store.prior`` re-checks eTag, size and bytes
DISCOVERED_RECEIPTS = "receipts/discovered"
# total seconds one run may sleep between retries of a throttled or failed GET
RETRY_BUDGET_SECONDS = 60
KINDS = ("original", "derivative", "archive_member", "attachment", "mail_message")
# The records' entry kinds (closed: an unknown kind fails the generation).
RECORD_KINDS = {
    "original submitted process map": "original",
    "original submitted process map package": "original",
    "original submitted business evidence archive": "original",
    "explicit case-linked business source file": "original",
    "supplied text export of correspondence": "derivative",
    "original supplied archive member": "archive_member",
    "attachment embedded in supplied EML": "attachment",
    "original submitted AO corpus file": "original",
    "original case-linked QRM evidence": "original",
    "original submitted AO corpus source alias": "original",
    "historical complete rendered artifact text, derived representation": "derivative",
}
ALIAS_KINDS = frozenset({"original submitted AO corpus source alias"})
# File rows: ``pinned`` (expected bytes and SHA-256), ``unpinned`` (a capture that names
# the provider's size and quickXorHash but not the SHA-256: the reader's full read pins
# it, ``--pin-receipts``), ``oversize`` (a capture over the reader's content bound,
# ``use_case_intake.MAX_CONTENT_BYTES``: typed and counted, never skipped), ``unfetched``
# (a pin whose bytes no guarded route reaches). Gap rows: ``gap``, ``superseded``.
FILE_STATUSES = ("pinned", "unpinned", "oversize", "unfetched")
ROW_STATUSES = (*FILE_STATUSES, "gap", "superseded")
# ``gone``: Graph answered 404 (the item or link no longer exists); ``no_route``: no
# guarded route of this reader reaches the bytes (detail names why); ``not_file``: a
# listed item without a file facet (a OneNote notebook or another package).
BODY_STATUSES = ("read", "partial", "capped", "mismatch", "denied", "gone", "unavailable",
                 "no_route", "not_file")
# Why an ``unfetched`` row has no route of its own. Each is read through a route made for
# it when the row names what that route needs (``_routed``): a historical revision with its
# drive, item and version id through ``graph.drive_item_version_content``; a historical
# mail pin (a message with its received time, or an attachment under one) in the mailbox
# ``MAILBOX_ENV`` names. Without that the row stays ``no_route`` with its reason
# (``mailbox_not_configured`` when only the mailbox is missing).
NO_ROUTE_REASONS = ("historical_revision", "historical_mail_pin")
CAPTURE_KIND = "advisory context original file"
_REPOSITORY_REF = re.compile(r"https://github\.com/([A-Za-z0-9_.\-]+)/([A-Za-z0-9_.\-]+)/blob/"
                             r"([0-9a-f]{40})/([A-Za-z0-9_.\-]+(?:/[A-Za-z0-9_.\-]+)*)")
EXTRACTION_STATUSES = ("complete", "partial", "unread", "not_run")
ROLES = ("verifier", "worker", "supervisor")
IDENTITY_ENV = {"account_uuid": "CLAUDE_CODE_ACCOUNT_UUID",
                "remote_session_id": "CLAUDE_CODE_REMOTE_SESSION_ID",
                "container_id": "CLAUDE_CODE_CONTAINER_ID"}
STATE_ENV = "PLX_WORKBENCH_STATE_DIR"
# The mailbox the historical receiving mail pins live in: the pins name none, and the same
# value already serves ``case.exposed_mail`` (scripts.eval_exposed_sources), so one human
# setting reaches both reads.
MAILBOX_ENV = "PLX_EVAL_EXPOSED_MAILBOX"
# A located pinned message is matched by its exact received second within this many
# messages listed oldest first from that second (one listing GET).
MAIL_LISTING_PAGE = 10
MAIL_BODY_CHARS = 1_000_000  # a located message's text body is read, stored and extracted
MAX_PART_BYTES = 64_000_000  # one archive member or attachment (a delivered mail is ~22 MB)
PAGE = 50  # the live verifier records at most 50 entries of one mapping
# The expected overrun past ``--budget-seconds``: the read in flight at the deadline
# (its download already started, then the eTag re-read, member listing and extraction
# without OCR), or the subfolders of a watch folder already being listed. A forecast,
# not enforced (the transport timeout is per socket operation): no download or OCR
# starts after the deadline, so the overrun is one step, not a queue, and a live-check
# spec's timeout covers the budget plus this bound.
STEP_BOUND_SECONDS = 240
WATCH_MAX_DEPTH = 5
WATCH_MAX_ITEMS = 500
READ_ONLY_GUARD = "GET-only; token POST only"
_HEX64 = re.compile(r"[0-9a-f]{64}")
_TOKEN = re.compile(r"[A-Za-z0-9_.:\-]{1,64}")
_UTC = timezone.utc


def _id(prefix: str, key: str) -> str:
    return f"{prefix}-{sha256(key.encode('utf-8')).hexdigest()[:12]}"


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _case_of(name: str) -> str:
    """The case a top-level submission names: its file stem before a dash separator."""
    stem = PurePosixPath(name).stem
    return _slug(re.split(r"\s+[—–-]\s+", stem, maxsplit=1)[0]) or "unnamed"


# ------------------------------------------------------------------ registry

def _proven(specs: Sequence[str]) -> dict[str, str]:
    """``CHECK_ID=FILE``: every ``sha256`` value in FILE is proven by that check."""
    proven: dict[str, str] = {}

    def walk(node: Any, check: str) -> None:
        if isinstance(node, Mapping):
            for key, value in node.items():
                if key == "sha256" and isinstance(value, str) and _HEX64.fullmatch(value):
                    proven[value] = check
                else:
                    walk(value, check)
        elif isinstance(node, list):
            for value in node:
                walk(value, check)

    for spec in specs:
        check, _, path = spec.partition("=")
        if not _TOKEN.fullmatch(check) or not path:
            raise ValueError("proven must be CHECK_ID=FILE")
        walk(json.loads(Path(path).read_text(encoding="utf-8")), check)
    return proven


def _proven_by(entry: Mapping[str, Any], proven: Mapping[str, str]) -> list[str]:
    """The checks that already proved this entry's bytes: a ``--proven`` pin of its
    SHA-256, or the record's own receipt when it states a recorded capture."""
    checks = {proven[entry["sha256"]]} if entry["sha256"] in proven else set()
    if (entry.get("claude_full_body_read_status") == "proven_by_recorded_capture"
            and isinstance(entry.get("claude_receipt_id"), str)):
        checks.add(entry["claude_receipt_id"])
    return sorted(checks)


def _advisory(record: Mapping[str, Any], raw: bytes, commit: str, path: str,
              role: str) -> dict[str, Any]:
    if record.get("schema") != ADVISORY_SCHEMA:
        raise ValueError(f"advisory_record_invalid:{role}")
    if not re.fullmatch(r"[0-9a-f]{40}", commit or "") or not path:
        raise ValueError(f"advisory_commit_invalid:{role}")
    receipt = record.get("source_inventory_receipt") or {}
    return {"role": role, "commit": commit, "path": path,
            "record_sha256": sha256(raw).hexdigest(),
            "source_revision": record.get("source_revision"),
            "source_id": record.get("source_id"), "created_at": record.get("created_at"),
            "inventory_observed_at": receipt.get("inventory_observed_at")
            or receipt.get("observed_at"),
            **({"supersession": record["supersession"]} if record.get("supersession") else {})}


def _file_row(entry: Mapping[str, Any], index: int, row_id: str, origin: str,
              proven: Mapping[str, str]) -> dict[str, Any]:
    kind = RECORD_KINDS.get(entry.get("kind"))
    if kind is None:
        raise ValueError(f"advisory_entry_kind_unknown:{origin}:{index}")
    if not _HEX64.fullmatch(str(entry.get("sha256"))) or type(entry.get("size_bytes")) \
            is not int:
        raise ValueError(f"advisory_entry_pin_invalid:{origin}:{index}")
    name = (entry.get("original_name") or entry.get("name")
            or PurePosixPath(entry["relative_path"]).name)
    versions = (entry.get("versions") or {}).get("value") or []
    return {
        "id": row_id, "status": "pinned", "origin": origin, "record_index": index,
        "owner": entry.get("owner"), "case": entry.get("case"), "case_ids": [],
        "kind": kind, "record_kind": entry["kind"], "parent": None, "alias_of": None,
        "linked_from": entry.get("linked_from"), "name": name,
        "member_path": entry.get("archive_member"),
        "mime_part_index": entry.get("mime_part_index"),
        "source": {"url": entry.get("source_url"),
                   "teams_message_url": entry.get("source_message_url"),
                   "teams_message_id": entry.get("source_message_id"),
                   "teams_attachment_id": entry.get("source_attachment_id"),
                   "drive_id": entry.get("drive_id"), "item_id": entry.get("item_id"),
                   "repository_ref": entry.get("repository_source")},
        "revision": {"version_id": entry.get("version_id") or entry.get("source_version"),
                     "etag": entry.get("etag"),
                     "modified_at": entry.get("modified_at") or entry.get("last_modified"),
                     "submitted_at": entry.get("submitted_at"),
                     "quick_xor_hash": entry.get("quick_xor_hash"),
                     "versions": [{"id": v.get("id"), "size": v.get("size"),
                                   "modified_at": v.get("last_modified_date_time")}
                                  for v in versions if isinstance(v, Mapping)]},
        "expected": {"bytes": entry["size_bytes"], "sha256": entry["sha256"]},
        "mime_type": entry.get("mime_type"),
        "detected_media_type": entry.get("detected_media_type"),
        "exposure": EXPOSURE, "proven_by": _proven_by(entry, proven),
        "record_body_status": entry.get("claude_full_body_read_status"),
        "note": entry.get("note") or entry.get("bytes_match_basis")}


def _mail_rows(pins: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Historical mail pins (message body hashes, attachment byte hashes): unfetched rows."""
    rows, messages = [], {}
    note = pins.get("status") if isinstance(pins.get("status"), str) else None
    for kind, items in (("mail_message", pins.get("mail_messages") or []),
                        ("attachment", pins.get("mail_attachments") or [])):
        for index, pin in enumerate(items):
            source_id = pin.get("source_id") if isinstance(pin, Mapping) else None
            if not isinstance(source_id, str) or not source_id:
                raise ValueError(f"advisory_mail_pin_invalid:{kind}:{index}")
            parent = None
            if kind == "attachment":
                parent = messages.get(pin.get("message"))
                if parent is None:
                    raise ValueError(f"advisory_mail_pin_parent_unresolved:{index}")
            sha = pin.get("sha256")
            rows.append({
                "id": _id("mp", source_id), "status": "unfetched", "origin": "delta",
                "unfetched_reason": "historical_mail_pin", "record_index": index,
                "owner": None, "case": None, "case_ids": [], "kind": kind,
                "record_kind": f"historical receiving {kind}", "parent": parent,
                "alias_of": None, "linked_from": None, "name": pin.get("name"),
                "member_path": None, "mime_part_index": None,
                "source": {"mail_message_id": pin.get("message_id"),
                           "mail_attachment_id": pin.get("attachment_id"),
                           "received_at": pin.get("received_at"), "id_type": pin.get("id_type"),
                           "pin_source_id": source_id, "evidence": pin.get("evidence")},
                "revision": {"version_id": None, "etag": None, "modified_at": None,
                             "submitted_at": None, "quick_xor_hash": None, "versions": []},
                "expected": {"bytes": pin.get("bytes"),
                             "sha256": sha if _HEX64.fullmatch(str(sha)) else None,
                             "body_sha256": pin.get("body_sha256")},
                "mime_type": None, "detected_media_type": None, "exposure": EXPOSURE,
                "proven_by": [], "record_body_status": None, "note": note})
            if kind == "mail_message":
                messages[source_id] = rows[-1]["id"]
    return rows


def _gap_rows(baseline: Sequence[Any], current: Sequence[Any] | None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    later = {}
    for origin, gaps in (("delta", current or []), ("baseline", baseline)):
        for index, gap in enumerate(gaps):
            if not isinstance(gap, Mapping) or not isinstance(gap.get("item"), str):
                raise ValueError(f"advisory_gap_invalid:{origin}:{index}")
            row = {"id": _id("gap", f"{origin}:{gap['item']}" if current else gap["item"]),
                   "status": "gap", "origin": origin, "gap_index": index,
                   "exposure": EXPOSURE, "gap": dict(gap)}
            if origin == "delta":
                later[gap["item"]] = row["id"]
            elif gap["item"] in later:  # the incremental record restates this gap
                row.update(status="superseded", superseded_by=later[gap["item"]])
            rows.append(row)
    return rows


def _watch(links: Sequence[str], drives: Sequence[Sequence[str]],
           rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Watch folders: a sharing/web URL (resolved through ``/shares/``) or a drive id and
    folder path (a nondefault library); ``derived_from`` lists the rows under it."""
    watch = []
    for url, drive, path in [*((u, None, None) for u in links),
                             *((w, d, p) for d, p, w in drives)]:
        if not url.startswith("https://") or (drive and not path.startswith("/")):
            raise ValueError("watch_folder_invalid")
        prefix = url.rstrip("/").replace(" ", "%20") + "/"
        folder = (path or "").rstrip("/") + "/"
        under = sorted(r["id"] for r in rows if r["status"] == "pinned" and ((
            r["source"].get("url") or "").replace(" ", "%20").startswith(prefix) or (
            drive and r["source"].get("drive_id") == drive
            and (r["source"].get("drive_path") or "").startswith(folder))))
        watch.append({"id": _id("wf", drive + ":" + path if drive else url),
                      "folder_url": url, "drive_id": drive, "path": path,
                      "max_depth": WATCH_MAX_DEPTH, "derived_from": under})
    return watch


def _capture_row(case: str, gap: str, raw: bytes, commit: str, path: str,
                 index: int) -> dict[str, Any]:
    """One capture (a ``plx-advisory/v1`` context record naming one ``original_file``):
    ``unpinned`` when it names no SHA-256 (the reader's full read pins it), ``oversize``
    when the provider's size is over the reader's content bound, else ``pinned``."""
    from operating.use_case_intake import MAX_CONTENT_BYTES

    if not _TOKEN.fullmatch(case or "") or not _TOKEN.fullmatch(gap or ""):
        raise ValueError(f"capture_case_or_gap_invalid:{index}")
    if not re.fullmatch(r"[0-9a-f]{40}", commit or "") or not path:
        raise ValueError(f"capture_commit_invalid:{index}")
    record = json.loads(raw)
    original = record.get("original_file") if isinstance(record, Mapping) else None
    if not isinstance(original, Mapping) or record.get("schema") != ADVISORY_SCHEMA:
        raise ValueError(f"capture_record_invalid:{index}")
    size, sha = original.get("bytes"), original.get("sha256")
    drive, item, url = original.get("drive_id"), original.get("item_id"), original.get(
        "source_url")
    if (type(size) is not int or size < 0 or not isinstance(original.get("name"), str)
            or not (isinstance(drive, str) and isinstance(item, str) and drive and item)
            or (sha is not None and not _HEX64.fullmatch(str(sha)))
            or (url is not None and not str(url).startswith("https://"))):
        raise ValueError(f"capture_original_invalid:{index}")
    status = "oversize" if size > MAX_CONTENT_BYTES else "pinned" if sha else "unpinned"
    row = {
        "id": _id("co", f"capture:{drive}/{item}"), "status": status, "origin": "capture",
        "capture_index": index,
        "capture": {"commit": commit, "path": path, "record_sha256": sha256(raw).hexdigest(),
                    "gap": gap, "source_revision": record.get("source_revision"),
                    "created_at": record.get("created_at")},
        "owner": None, "case": case, "case_ids": [case], "kind": "original",
        "record_kind": CAPTURE_KIND, "parent": None, "alias_of": None, "linked_from": None,
        "name": original["name"], "member_path": None, "mime_part_index": None,
        "source": {"url": url, "teams_message_url": None, "teams_message_id": None,
                   "teams_attachment_id": None, "drive_id": drive, "item_id": item,
                   "repository_ref": None},
        "revision": {"version_id": original.get("version"), "etag": None,
                     "modified_at": original.get("modified_at"), "submitted_at": None,
                     "quick_xor_hash": original.get("quickXorHash"), "versions": []},
        "expected": {"bytes": size, "sha256": sha},
        "mime_type": None, "detected_media_type": None, "exposure": EXPOSURE,
        "proven_by": [], "record_body_status": original.get("body_delivery_status"),
        "note": "capture: provider size and quickXorHash only; the SHA-256 and member "
                "manifest come from the reader's full read receipt (--pin-receipts)",
        "member_manifest": None}
    if status == "oversize":
        row["oversize"] = {"bytes": size, "bound": MAX_CONTENT_BYTES,
                           "bound_name": "use_case_intake.MAX_CONTENT_BYTES"}
    return row


_MANIFEST_STATUSES = ("read", "oversize", "unread")


def _pin_receipts(rows: list[dict[str, Any]], receipts: Sequence[Mapping[str, Any]]) -> None:
    """Pin each ``unpinned`` capture row a full-read receipt verified (body read, size and
    quickXorHash matched): its SHA-256 and member manifest, and one ``archive_member`` row
    per member (``oversize`` for a member over the reader's part bound). A receipt for a
    capture row whose read did not match fails: a person decides, nothing is guessed."""
    by_id = {row["id"]: row for row in rows if row.get("origin") == "capture"}
    for receipt in receipts:
        row = by_id.get(receipt.get("entry_id")) if isinstance(receipt, Mapping) else None
        if row is None or row["status"] != "unpinned" or receipt.get("status") != "registered":
            continue
        if receipt.get("schema") != RECEIPT_SCHEMA:
            raise ValueError(f"capture_receipt_invalid:{row['id']}")
        if (receipt.get("body_status") != "read" or receipt.get("match") is not True
                or receipt.get("bytes") != row["expected"]["bytes"]
                or not _HEX64.fullmatch(str(receipt.get("sha256")))):
            raise ValueError(f"capture_receipt_unverified:{row['id']}")
        manifest = receipt.get("member_manifest")
        members = (manifest or {}).get("members") if isinstance(manifest, Mapping) else []
        if manifest is not None and not isinstance(members, list):
            raise ValueError(f"capture_manifest_invalid:{row['id']}")
        row.update(status="pinned", pinned_from={
            "registry_version": receipt.get("registry_version"),
            "read_at": receipt.get("read_at"),
            "run_id": (receipt.get("reader") or {}).get("run_id")})
        row["expected"] = {"bytes": receipt["bytes"], "sha256": receipt["sha256"]}
        row["detected_media_type"] = receipt.get("detected_media_type")
        row["member_manifest"] = {"members": [], "counts": {s: 0 for s in _MANIFEST_STATUSES}}
        children: list[dict[str, Any]] = []
        for member in members or []:
            if (not isinstance(member, Mapping) or member.get("status") not in
                    _MANIFEST_STATUSES or not isinstance(member.get("path"), str)
                    or not member["path"] or not isinstance(member.get("name"), str)
                    or (member["status"] == "read" and not (
                        _HEX64.fullmatch(str(member.get("sha256")))
                        and type(member.get("bytes")) is int))):
                raise ValueError(f"capture_manifest_invalid:{row['id']}")
            entry = {k: member.get(k) for k in ("path", "name", "bytes", "sha256", "kind",
                                                "status")}
            row["member_manifest"]["members"].append(entry)
            row["member_manifest"]["counts"][member["status"]] += 1
            if member["status"] == "unread":
                continue
            child = {
                **{k: row[k] for k in ("owner", "case", "exposure")},
                "id": _id("co", f"{row['id']}!/{member['path']}"),
                "status": "pinned" if member["status"] == "read" else "oversize",
                "origin": "capture", "capture_index": row["capture_index"],
                "case_ids": list(row["case_ids"]), "kind": "archive_member",
                "record_kind": "capture archive member", "parent": row["id"],
                "alias_of": None, "linked_from": None, "name": member["name"],
                "member_path": member["path"], "mime_part_index": None,
                "source": {"url": None, "drive_id": None, "item_id": None},
                "revision": {"version_id": None, "etag": None, "modified_at": None,
                             "submitted_at": None, "quick_xor_hash": None, "versions": []},
                "expected": {"bytes": member.get("bytes"), "sha256": member.get("sha256")},
                "mime_type": None, "detected_media_type": _MEDIA_TYPES.get(member.get("kind")),
                "proven_by": [], "record_body_status": None,
                "note": "member of a capture archive, pinned from the reader's receipt"}
            if child["status"] == "oversize":
                child["oversize"] = {"bytes": member.get("bytes"), "bound": member.get("bound"),
                                     "bound_name": "case_originals.MAX_PART_BYTES"}
            children.append(child)
        at = rows.index(row) + 1
        rows[at:at] = children


# -- manifest records: the Dot's read of a SharePoint library (source / nested / zip)
MANIFEST_SECTIONS = ("source_manifest", "nested_attachment_manifest", "zip_member_manifest")
_MANIFEST_KEYS = {
    "source_manifest": (frozenset({"canonical_url", "list_id", "list_item_id",
                                   "list_item_unique_id", "name", "revision", "sha256",
                                   "site_id", "size_bytes"}), frozenset()),
    "nested_attachment_manifest": (frozenset({"bytes", "name", "parent_list_item_id",
                                              "parent_sha256", "sha256", "storage_prefix"}),
                                   frozenset()),
    "zip_member_manifest": (frozenset({"bytes", "matching_top_level_list_item_ids", "name",
                                       "sha256"}), frozenset({"parent_sha256"}))}
_REVISION_KEYS = frozenset({"list_etag", "modified_at", "version"})
_GUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_GUID_RE = re.compile(_GUID)
_ETAG_RE = re.compile(r'"\{?(' + _GUID + r')\}?,\d+"')
_HOST_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9\-]{1,63})+")
_LIST_ITEM_RE = re.compile(r"[0-9]{1,12}")
CAVEATS_SCHEMA = "plx-case-originals-caveats/v1"
CROSSWALK_FORMAT = "plx-eval-case-card-proposals/v1"
CROSSWALK_KINDS = ("watched_source", "descriptor", "registry_reference")
_ADVISORY_ID = re.compile(r"advisory:[0-9a-f]{12}")
_REPO_PATH = re.compile(r"[A-Za-z0-9_.\-]+(?:/[A-Za-z0-9_.\-]+)*")


def _plain_name(name: Any) -> bool:
    return (isinstance(name, str) and 0 < len(name) <= 255 and name == name.strip()
            and name not in (".", "..") and "/" not in name and "\\" not in name
            and not any(ord(c) < 32 or ord(c) == 127 for c in name))


def _member_name(name: Any) -> bool:
    return (isinstance(name, str) and 0 < len(name) <= 1024 and not name.startswith("/")
            and all(_plain_name(part) for part in name.split("/")))


def _size(value: Any) -> bool:
    return type(value) is int and value >= 0


def _drive_guids(drive_id: str) -> tuple[str, str, str] | None:
    """The site, web and list GUIDs a ``b!`` drive id encodes (base64url of their
    little-endian bytes), or None."""
    import base64
    import uuid

    if not isinstance(drive_id, str) or not drive_id.startswith("b!"):
        return None
    body = drive_id[2:]
    try:
        raw = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
    except (ValueError, TypeError):
        return None
    if len(raw) != 48:
        return None
    site, web, lst = (str(uuid.UUID(bytes_le=raw[i:i + 16])) for i in (0, 16, 32))
    return site, web, lst


def _manifest_entry_ok(section: str, entry: Any) -> bool:
    required, optional = _MANIFEST_KEYS[section]
    if not isinstance(entry, Mapping) or not required <= set(entry) \
            or not set(entry) <= required | optional:
        return False
    if not _HEX64.fullmatch(str(entry["sha256"])):
        return False
    if section == "source_manifest":
        site = str(entry["site_id"]).split(",")
        revision = entry["revision"]
        unique = entry["list_item_unique_id"]
        if not (_plain_name(entry["name"]) and _size(entry["size_bytes"])
                and isinstance(entry["list_item_id"], str)
                and _LIST_ITEM_RE.fullmatch(entry["list_item_id"])
                and isinstance(unique, str) and _GUID_RE.fullmatch(unique)
                and isinstance(entry["list_id"], str) and _GUID_RE.fullmatch(entry["list_id"])
                and len(site) == 3 and _HOST_RE.fullmatch(site[0])
                and all(_GUID_RE.fullmatch(s) for s in site[1:])
                and isinstance(entry["canonical_url"], str)
                and entry["canonical_url"].startswith("https://")
                and isinstance(revision, Mapping) and set(revision) <= _REVISION_KEYS
                and all(isinstance(revision.get(k), (str, type(None)))
                        for k in _REVISION_KEYS)):
            return False
        from urllib.parse import unquote, urlsplit

        if not unquote(urlsplit(entry["canonical_url"]).path).endswith("/" + entry["name"]):
            return False
        etag = revision.get("list_etag")
        if etag is not None:
            found = _ETAG_RE.fullmatch(etag)
            if not found or found.group(1).lower() != unique.lower():
                return False
        return True
    if not _size(entry["bytes"]):
        return False
    if section == "nested_attachment_manifest":
        prefix = entry["storage_prefix"]
        return (_plain_name(entry["name"]) and isinstance(entry["parent_list_item_id"], str)
                and bool(_LIST_ITEM_RE.fullmatch(entry["parent_list_item_id"]))
                and bool(_HEX64.fullmatch(str(entry["parent_sha256"])))
                and isinstance(prefix, str) and 0 < len(prefix) <= 255
                and ".." not in prefix and not any(ord(c) < 32 for c in prefix))
    matches = entry["matching_top_level_list_item_ids"]
    return (_member_name(entry["name"]) and isinstance(matches, list)
            and all(isinstance(m, str) and _LIST_ITEM_RE.fullmatch(m) for m in matches)
            and len(set(matches)) == len(matches)
            and ("parent_sha256" not in entry
                 or bool(_HEX64.fullmatch(str(entry["parent_sha256"])))))


def _manifest_route(entry: Mapping[str, Any], drives: Sequence[Sequence[str]]
                    ) -> tuple[str, str] | None:
    """The watched drive a listed file lives in and its path there: the drive whose
    ``b!`` id encodes the entry's site, web and list GUIDs and whose web URL the entry's
    canonical URL is under (one answer, else None)."""
    from urllib.parse import unquote, urlsplit

    host, site, web = entry["site_id"].split(",")
    url = entry["canonical_url"].replace(" ", "%20")
    found = set()
    for drive, path, web_url in drives:
        guids = _drive_guids(drive)
        if guids is None or guids != (site.lower(), web.lower(), entry["list_id"].lower()):
            continue
        prefix = web_url.rstrip("/").replace(" ", "%20") + "/"
        if urlsplit(prefix).hostname != host.lower() or not url.startswith(prefix):
            continue
        rest = unquote(url[len(prefix):])
        if not _member_name(rest) or not path.startswith("/"):
            continue
        found.add((drive, path.rstrip("/") + "/" + rest))
    return found.pop() if len(found) == 1 else None


def _manifest_rows(case: str, raw: bytes, commit: str, path: str, index: int,
                   drives: Sequence[Sequence[str]]
                   ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """One manifest record (``plx-advisory/v1`` with ``source_manifest``,
    ``nested_attachment_manifest`` and ``zip_member_manifest``): its advisory header and
    one pinned row per entry. A source file is routed to the watched drive its list is
    (drive id and drive path; no URL is kept), an attachment names its message, a zip
    member its archive. Anything unknown or unresolved fails closed."""
    if not _TOKEN.fullmatch(case or "") or not re.fullmatch(r"[0-9a-f]{40}", commit or "") \
            or not path:
        raise ValueError(f"manifest_case_or_commit_invalid:{index}")
    try:
        record = json.loads(raw)
    except ValueError:
        raise ValueError("manifest_record_invalid") from None
    if not isinstance(record, Mapping) or record.get("schema") != ADVISORY_SCHEMA:
        raise ValueError("manifest_record_invalid")
    for key in record:
        if re.search(r"manifest|member|attachment", key, re.I) and key not in MANIFEST_SECTIONS:
            raise ValueError(f"manifest_section_unknown:{key[:40]}")
    sections = {s: record.get(s, [] if s != "source_manifest" else None)
                for s in MANIFEST_SECTIONS}
    for section, entries in sections.items():
        if not isinstance(entries, list) or (section == "source_manifest" and not entries):
            raise ValueError(f"manifest_section_invalid:{section}")
        for i, entry in enumerate(entries):
            if not _manifest_entry_ok(section, entry):
                raise ValueError(f"manifest_entry_invalid:{section}:{i}")
    sources = sections["source_manifest"]
    scope = record.get("scope") if isinstance(record.get("scope"), Mapping) else {}
    for key, actual in (("top_level_originals", len(sources)),
                        ("unique_top_level_payloads", len({e["sha256"] for e in sources})),
                        ("nested_attachments_hashed",
                         len(sections["nested_attachment_manifest"])),
                        ("total_original_bytes", sum(e["size_bytes"] for e in sources))):
        if key in scope and scope[key] != actual:
            raise ValueError(f"manifest_count_mismatch:{key}")
    blank_revision = {"version_id": None, "etag": None, "modified_at": None,
                      "submitted_at": None, "quick_xor_hash": None, "versions": []}

    def row(section: str, i: int, row_id: str, kind: str, entry: Mapping[str, Any],
            size: int, **fields: Any) -> dict[str, Any]:
        return {"id": row_id, "status": "pinned", "origin": "manifest",
                "manifest_index": index, "record_section": section, "record_index": i,
                "owner": None, "case": case, "case_ids": [case], "kind": kind,
                "record_kind": f"manifest {section.split('_manifest')[0].replace('_', ' ')}",
                "parent": None, "alias_of": None, "same_bytes_as": [], "linked_from": None,
                "name": entry["name"], "member_path": None, "mime_part_index": None,
                "source": {"url": None, "drive_id": None, "item_id": None},
                "revision": dict(blank_revision),
                "expected": {"bytes": size, "sha256": entry["sha256"]},
                "mime_type": None, "detected_media_type": None, "exposure": EXPOSURE,
                "proven_by": [], "record_body_status": None, **fields}

    rows: list[dict[str, Any]] = []
    by_item: dict[str, dict[str, Any]] = {}
    seen: set[Any] = set()
    for i, entry in enumerate(sources):
        unique = entry["list_item_unique_id"].lower()
        if (entry["list_id"].lower(), unique) in seen or entry["list_item_id"] in by_item:
            raise ValueError(f"manifest_entry_duplicate:source_manifest:{i}")
        seen.add((entry["list_id"].lower(), unique))
        route = _manifest_route(entry, drives)
        if route is None:
            raise ValueError(f"manifest_entry_unrouted:source_manifest:{i}")
        revision = entry["revision"]
        rows.append(row("source_manifest", i, _id("co", f"manifest:{entry['list_id'].lower()}"
                                                        f"/{unique}"),
                        "original", entry, entry["size_bytes"]))
        rows[-1]["source"] = {
            "url": None, "teams_message_url": None, "teams_message_id": None,
            "teams_attachment_id": None, "drive_id": route[0], "drive_path": route[1],
            "item_id": None, "repository_ref": None, "site_id": entry["site_id"],
            "list_id": entry["list_id"], "list_item_id": entry["list_item_id"],
            "list_item_unique_id": entry["list_item_unique_id"]}
        rows[-1]["revision"].update(version_id=revision.get("version"),
                                    etag=revision.get("list_etag"),
                                    modified_at=revision.get("modified_at"))
        by_item[entry["list_item_id"]] = rows[-1]
    containers = list(rows)
    for i, entry in enumerate(sections["nested_attachment_manifest"]):
        parent = by_item.get(entry["parent_list_item_id"])
        if parent is None or parent["expected"]["sha256"] != entry["parent_sha256"]:
            raise ValueError(f"manifest_parent_unresolved:nested_attachment_manifest:{i}")
        row_id = _id("co", f"{parent['id']}!/{entry['storage_prefix']}")
        if row_id in seen:
            raise ValueError(f"manifest_entry_duplicate:nested_attachment_manifest:{i}")
        seen.add(row_id)
        rows.append(row("nested_attachment_manifest", i, row_id, "attachment", entry,
                        entry["bytes"], parent=parent["id"],
                        msg_storage=entry["storage_prefix"]))
        containers.append(rows[-1])
    for i, entry in enumerate(sections["zip_member_manifest"]):
        if "parent_sha256" in entry:
            parents = [r for r in containers
                       if r["expected"]["sha256"] == entry["parent_sha256"]]
        else:
            parents = [r for r in containers if r["name"].lower().endswith(".zip")]
        if len(parents) != 1:
            raise ValueError(f"manifest_parent_unresolved:zip_member_manifest:{i}")
        matches = [by_item.get(m) for m in entry["matching_top_level_list_item_ids"]]
        if any(m is None or m["expected"]["sha256"] != entry["sha256"] for m in matches):
            raise ValueError(f"manifest_match_unresolved:zip_member_manifest:{i}")
        row_id = _id("co", f"{parents[0]['id']}!/{entry['name']}")
        if row_id in seen:
            raise ValueError(f"manifest_entry_duplicate:zip_member_manifest:{i}")
        seen.add(row_id)
        rows.append(row("zip_member_manifest", i, row_id, "archive_member", entry,
                        entry["bytes"], parent=parents[0]["id"], member_path=entry["name"],
                        matching_rows=sorted(m["id"] for m in matches)))
    payloads = {s: len({e["sha256"] for e in sections[s]}) for s in MANIFEST_SECTIONS}
    payloads["all"] = len({r["expected"]["sha256"] for r in rows})
    head = {"role": "manifest", "commit": commit, "path": path,
            "record_sha256": sha256(raw).hexdigest(),
            "source_revision": record.get("source_revision"),
            "source_id": record.get("source_id"), "created_at": record.get("created_at"),
            "inventory_observed_at": record.get("observed_at"), "case": case,
            "sections": {s: len(sections[s]) for s in MANIFEST_SECTIONS},
            "payloads": payloads}
    return head, rows


def _same_bytes(rows: Sequence[dict[str, Any]]) -> None:
    """Each manifest row: ``alias_of`` the first pinned row before it with the same bytes,
    ``same_bytes_as`` every other pinned row with them (identities kept, one payload).
    Rows of the earlier records are not changed."""
    by_sha: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        if row["status"] == "pinned":
            by_sha.setdefault(row["expected"]["sha256"], []).append(row)
    for row in rows:
        if row.get("origin") != "manifest":
            continue
        same = by_sha[row["expected"]["sha256"]]
        first = same[0]
        row["alias_of"] = None if first is row else first["id"]
        row["same_bytes_as"] = sorted(r["id"] for r in same if r is not row)


def _caveats(advisories: Sequence[dict[str, Any]], notes: Sequence[bytes]) -> None:
    """Owner caveats (``plx-case-originals-caveats/v1``) bound to one advisory record by
    its SHA-256, kept verbatim in that advisory's header."""
    for raw in notes:
        try:
            note = json.loads(raw)
        except ValueError:
            raise ValueError("caveats_invalid") from None
        items = note.get("caveats") if isinstance(note, Mapping) else None
        if (not isinstance(note, Mapping)
                or set(note) != {"schema", "directive", "advisory_record_sha256", "caveats"}
                or note["schema"] != CAVEATS_SCHEMA
                or not re.fullmatch(r"[0-9a-f]{12}", str(note["directive"]))
                or not _HEX64.fullmatch(str(note["advisory_record_sha256"]))
                or not isinstance(items, list) or not items
                or any(not isinstance(c, Mapping) or set(c) != {"id", "text"}
                       or not re.fullmatch(r"[a-z0-9]{1,8}", str(c["id"]))
                       or not isinstance(c["text"], str) or not c["text"].strip()
                       for c in items)
                or len({c["id"] for c in items}) != len(items)):
            raise ValueError("caveats_invalid")
        target = [a for a in advisories
                  if a["record_sha256"] == note["advisory_record_sha256"]]
        if len(target) != 1:
            raise ValueError("caveats_advisory_unknown")
        if "caveats" in target[0]:
            raise ValueError("caveats_invalid")
        target[0]["caveats"] = {"directive": note["directive"],
                                "items": [{"id": c["id"], "text": c["text"]} for c in items]}


def _crosswalk(inputs: Sequence[tuple[str, bytes]], rows: Sequence[Mapping[str, Any]],
               watch: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Each source an eval card file names (``plx-eval-case-card-proposals/v1``): its
    watched originals, any other descriptor of one file (item id, SHA-256, bytes) and
    the registry rows it cites, each with the record that names it and the registry row
    it maps to, or the reason it maps to none. Ids, names and hashes only: no location,
    URL or body is copied."""
    files = [r for r in rows if r["status"] in FILE_STATUSES]
    known = {r["id"] for r in rows}
    folders = {w["id"] for w in watch}
    out: list[dict[str, Any]] = []
    described: list[dict[str, Any]] = []

    def mapped(item: str, sha: str, kind: str, drive: str | None) -> dict[str, Any]:
        same_item = [r for r in files if r["source"].get("item_id") == item
                     and (drive is None or r["source"].get("drive_id") == drive)]
        exact = [r for r in same_item if r["expected"].get("sha256") == sha]
        if exact:
            return {"registry_row": exact[0]["id"], "match_basis": "item_id+sha256",
                    "unmapped_reason": None, "candidates": []}
        if same_item:
            reason, candidates = "registry_row_bytes_differ", same_item
        else:
            candidates = [r for r in files if r["status"] == "pinned"
                          and r["expected"].get("sha256") == sha]
            reason = ("bytes_match_other_identity" if candidates else
                      "watched_not_pinned" if kind == "watched_source"
                      else "descriptor_not_pinned")
        return {"registry_row": None, "match_basis": None, "unmapped_reason": reason,
                "candidates": sorted(r["id"] for r in candidates)}

    for path, raw in inputs:
        if not isinstance(path, str) or not _REPO_PATH.fullmatch(path) \
                or ".." in path.split("/"):
            raise ValueError("crosswalk_input_invalid")
        try:
            document = json.loads(raw)
        except ValueError:
            raise ValueError("crosswalk_input_invalid") from None
        source = document.get("source") if isinstance(document, Mapping) else None
        if (not isinstance(document, Mapping) or document.get("format") != CROSSWALK_FORMAT
                or not _TOKEN.fullmatch(str(document.get("case")))
                or not isinstance(source, Mapping)):
            raise ValueError("crosswalk_input_invalid")
        case = document["case"]
        described.append({"path": path, "sha256": sha256(raw).hexdigest(), "case": case})
        advisory = source.get("advisory")
        record = advisory.get("id") if isinstance(advisory, Mapping) else None
        if advisory is not None and not _ADVISORY_ID.fullmatch(str(record)):
            raise ValueError("crosswalk_record_invalid")
        made: list[dict[str, Any]] = []

        def add(kind: str, group: str, record_id: Any, item: Any = None, sha: Any = None,
                size: Any = None, name: Any = None, folder: Any = None,
                **match: Any) -> None:
            if not _ADVISORY_ID.fullmatch(str(record_id)):
                raise ValueError("crosswalk_record_invalid")
            key = f"{path}:{kind}:{group}:{item or match.get('registry_row')}"
            made.append({"id": _id("cx", key), "case": case, "group": group,
                         "record": record_id, "kind": kind, "item_id": item, "sha256": sha,
                         "bytes": size, "name": name, "watch_folder": folder,
                         **(match or mapped(item, sha, kind, None))})

        for key, value in source.items():
            if key in ("advisory", "related_advisories"):
                continue
            if key == "originals_registry":
                if isinstance(value, str):
                    continue
                cited = value.get("rows") if isinstance(value, Mapping) else None
                if not isinstance(cited, Mapping):
                    raise ValueError("crosswalk_source_unknown")
                for row_id in cited:
                    if row_id not in known:
                        raise ValueError("crosswalk_registry_row_unknown")
                    add("registry_reference", key, record, registry_row=row_id,
                        match_basis="row_id", unmapped_reason=None, candidates=[])
                continue
            if key == "watched_originals":
                listed = value.get("files") if isinstance(value, Mapping) else None
                groups = value.get("folders") if isinstance(value, Mapping) else None
                if (not isinstance(listed, list) or not isinstance(groups, Mapping)
                        or not set(value) <= {"note", "folders", "files"}):
                    raise ValueError("crosswalk_source_unknown")
                if any(g not in folders for g in groups.values()):
                    raise ValueError("crosswalk_watch_folder_unknown")
                for entry in listed:
                    if (not isinstance(entry, Mapping)
                            or set(entry) != {"case", "name", "item_id", "sha256", "bytes",
                                              "advisory"}
                            or entry["case"] not in groups
                            or not isinstance(entry["item_id"], str)
                            or not _TOKEN.fullmatch(entry["item_id"])
                            or not _HEX64.fullmatch(str(entry["sha256"]))
                            or not _size(entry["bytes"]) or not _plain_name(entry["name"])
                            or not _ADVISORY_ID.fullmatch(str(entry["advisory"]))):
                        raise ValueError("crosswalk_entry_invalid")
                    add("watched_source", entry["case"], entry["advisory"],
                        entry["item_id"], entry["sha256"], entry["bytes"], entry["name"],
                        groups[entry["case"]])
                continue
            if isinstance(value, Mapping) and "item_id" in value and "sha256" in value:
                item, sha, drive = value["item_id"], value["sha256"], value.get("drive_id")
                if (not isinstance(item, str) or not _TOKEN.fullmatch(item)
                        or not _HEX64.fullmatch(str(sha))
                        or not _size(value.get("bytes"))
                        or not (drive is None or isinstance(drive, str))
                        or not (value.get("name") is None or _plain_name(value["name"]))):
                    raise ValueError("crosswalk_entry_invalid")
                add("descriptor", key, record, item, sha, value["bytes"], value.get("name"),
                    **mapped(item, sha, "descriptor", drive))
                continue
            raise ValueError("crosswalk_source_unknown")
        if not made:
            raise ValueError("crosswalk_input_empty")
        out.extend(made)
    if len({r["id"] for r in out}) != len(out):
        raise ValueError("crosswalk_entry_duplicate")
    reasons: dict[str, int] = {}
    for r in out:
        if r["unmapped_reason"]:
            reasons[r["unmapped_reason"]] = reasons.get(r["unmapped_reason"], 0) + 1
    return {"inputs": described, "rows": out,
            "counts": {"rows": len(out), "mapped": sum(bool(r["registry_row"]) for r in out),
                       "unmapped": sum(not r["registry_row"] for r in out),
                       "by_kind": {k: sum(r["kind"] == k for r in out)
                                   for k in CROSSWALK_KINDS},
                       "by_reason": reasons}}


# -- card sources: originals an eval case card rests on that no source-pin record lists
CARD_SOURCES_SCHEMA = "plx-case-originals-card-sources/v1"
CARD_ATTACHMENT_KIND = "teams chat attachment"
_CARD_GAP_KEYS = frozenset({"item", "owner", "source", "evidence", "status"})
_CARD_ATTACHMENT_KEYS = frozenset({"name", "owner", "url", "teams_message_url",
                                   "attachment_id", "submitted_at", "bytes", "sha256",
                                   "advisory"})
# A claude.ai artifact held as its complete rendered text inside an advisory record
# committed to a repository (``source_text``; ``source_revision`` the SHA-256 of its UTF-8
# bytes): a derivative read from that record at its commit, like the delta's rendered maps.
CARD_RENDERED_KIND = "historical complete rendered artifact text, derived representation"
_CARD_RENDERED_KEYS = frozenset({"name", "owner", "repository", "bytes", "sha256",
                                 "advisory"})
_REPOSITORY_NAME = re.compile(r"[A-Za-z0-9_\-][A-Za-z0-9_.\-]{0,99}/[A-Za-z0-9_\-][A-Za-z0-9_.\-]{0,99}")
_TEAMS_LINK = re.compile(r"/l/message/([^/?#]+)/([0-9]{1,20})")
_CHAT_THREAD = re.compile(r"19:[A-Za-z0-9_.\-]{1,200}@[A-Za-z0-9_.\-]{1,64}")


def _teams_message(url: Any, *, chat_context: bool) -> tuple[str, str] | None:
    """(chat id, message id) of a Teams message link (``chat_context``: only a link whose
    ``context`` names a chat, the link the serving gate reads), else None."""
    from urllib.parse import parse_qs, unquote, urlsplit

    if not isinstance(url, str):
        return None
    try:
        parts = urlsplit(url)
        context = json.loads((parse_qs(parts.query).get("context") or ["{}"])[0])
    except ValueError:
        return None
    found = _TEAMS_LINK.fullmatch(parts.path)
    if parts.scheme != "https" or parts.hostname != "teams.microsoft.com" or found is None:
        return None
    chat = unquote(found.group(1))
    if not _CHAT_THREAD.fullmatch(chat) or (chat_context and not (
            isinstance(context, Mapping) and context.get("contextType") == "chat")):
        return None
    return chat, found.group(2)


def _card_rows(raw: bytes, records: Mapping[tuple[str, str], bytes]
               ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """(advisory headers, file rows, gap rows) of a card-sources input
    (``plx-case-originals-card-sources/v1``): the originals an eval case card names that
    no source-pin record lists. A ``gap`` entry is a ``gap`` row of origin ``card`` (no
    route; its ``expected`` pin, when known). A ``chat_attachment`` entry is a ``pinned``
    original shared as a Teams chat attachment: read through the attachment's content URL
    (the sharing route ``read`` follows for every such row) and served behind the gate of
    the chat message (``teams_message_url``, a chat link). Its pin is checked against the
    advisory record it names (``records``: (commit, path) -> bytes): the record's
    ``source_id`` is the URL, its ``source_revision`` the SHA-256 and one evidence source
    the same chat message; anything else fails closed. Only the size is the input's own
    claim, and the reader's full read checks it with the SHA-256. A ``rendered_artifact``
    entry is a ``pinned`` derivative held as complete rendered text in the advisory record
    it names (``_card_rendered``)."""
    from operating.use_case_intake import MAX_CONTENT_BYTES

    try:
        document = json.loads(raw)
    except ValueError:
        raise ValueError("card_sources_invalid") from None
    entries = document.get("entries") if isinstance(document, Mapping) else None
    if (not isinstance(document, Mapping) or document.get("schema") != CARD_SOURCES_SCHEMA
            or set(document) - {"schema", "note", "entries"} or not isinstance(entries, list)):
        raise ValueError("card_sources_invalid")
    advisories: list[dict[str, Any]] = []
    files: list[dict[str, Any]] = []
    gaps: list[dict[str, Any]] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, Mapping) or len(entry) != 2 or "case" not in entry \
                or not _TOKEN.fullmatch(str(entry["case"])):
            raise ValueError(f"card_source_invalid:{index}")
        case = entry["case"]
        if "gap" in entry:
            gap = entry["gap"]
            expected = {"bytes": None, "sha256": None}
            if isinstance(gap, Mapping) and "expected" in gap:
                expected = gap["expected"]
                gap = {k: v for k, v in gap.items() if k != "expected"}
                if (not isinstance(expected, Mapping) or set(expected) != {"bytes", "sha256"}
                        or (expected["sha256"] is not None
                            and not _HEX64.fullmatch(str(expected["sha256"])))
                        or (expected["bytes"] is not None
                            and (type(expected["bytes"]) is not int or expected["bytes"] < 0))):
                    raise ValueError(f"card_gap_invalid:{index}")
            if (not isinstance(gap, Mapping) or "item" not in gap or set(gap) - _CARD_GAP_KEYS
                    or any(not isinstance(v, str) or not v.strip() for v in gap.values())):
                raise ValueError(f"card_gap_invalid:{index}")
            gaps.append({"id": _id("gap", f"card:{case}:{gap['item']}"), "status": "gap",
                         "origin": "card", "case": case, "gap_index": index,
                         "exposure": EXPOSURE, "gap": dict(gap),
                         "expected": {"bytes": expected["bytes"],
                                      "sha256": expected["sha256"]}})
            continue
        if "rendered_artifact" in entry:
            row, header = _card_rendered(index, case, entry["rendered_artifact"], records,
                                         MAX_CONTENT_BYTES)
            if all(a["record_sha256"] != header["record_sha256"] for a in advisories):
                advisories.append(header)
            files.append(row)
            continue
        found = entry.get("chat_attachment")
        if not isinstance(found, Mapping) or set(found) != _CARD_ATTACHMENT_KEYS:
            raise ValueError(f"card_source_invalid:{index}")
        advisory = found["advisory"]
        message = _teams_message(found["teams_message_url"], chat_context=True)
        if (not isinstance(advisory, Mapping) or set(advisory) != {"commit", "path"}
                or not re.fullmatch(r"[0-9a-f]{40}", str(advisory["commit"]))
                or not isinstance(advisory["path"], str)
                or not _REPO_PATH.fullmatch(advisory["path"])
                or ".." in advisory["path"].split("/")
                or message is None
                or not _plain_name(found["name"])
                or not isinstance(found["owner"], str) or not found["owner"].strip()
                or not isinstance(found["url"], str) or not found["url"].startswith("https://")
                or not re.fullmatch(r"[A-Za-z0-9\-]{1,64}", str(found["attachment_id"]))
                or _instant(found["submitted_at"]) is None
                or type(found["bytes"]) is not int or found["bytes"] < 0
                or not _HEX64.fullmatch(str(found["sha256"]))):
            raise ValueError(f"card_attachment_invalid:{index}")
        if found["bytes"] > MAX_CONTENT_BYTES:
            raise ValueError(f"card_attachment_oversize:{index}")
        record_raw = records.get((advisory["commit"], advisory["path"]))
        if record_raw is None:
            raise ValueError(f"card_advisory_missing:{index}")
        try:
            record = json.loads(record_raw)
        except ValueError:
            raise ValueError(f"card_advisory_invalid:{index}") from None
        if not isinstance(record, Mapping):
            raise ValueError(f"card_advisory_invalid:{index}")
        evidence = record.get("evidence") if isinstance(record.get("evidence"), list) else []
        named = {_teams_message(e.get("source"), chat_context=False)
                 for e in evidence if isinstance(e, Mapping)}
        if (record.get("source_id") != found["url"]
                or record.get("source_revision") != f"sha256:{found['sha256']}"
                or message not in named):
            raise ValueError(f"card_attachment_unproven:{index}")
        header = _advisory(record, record_raw, advisory["commit"], advisory["path"], "card")
        if all(a["record_sha256"] != header["record_sha256"] for a in advisories):
            advisories.append(header)
        files.append({
            "id": _id("co", f"card:{case}:{found['url']}"), "status": "pinned",
            "origin": "card", "card_index": index, "owner": found["owner"], "case": case,
            "case_ids": [case], "kind": "original", "record_kind": CARD_ATTACHMENT_KIND,
            "parent": None, "alias_of": None, "linked_from": None, "name": found["name"],
            "member_path": None, "mime_part_index": None,
            "source": {"url": found["url"], "teams_message_url": found["teams_message_url"],
                       "teams_message_id": message[1],
                       "teams_attachment_id": found["attachment_id"], "drive_id": None,
                       "item_id": None, "repository_ref": None},
            "revision": {"version_id": None, "etag": None, "modified_at": None,
                         "submitted_at": found["submitted_at"], "quick_xor_hash": None,
                         "versions": []},
            "expected": {"bytes": found["bytes"], "sha256": found["sha256"]},
            "mime_type": None, "detected_media_type": None, "exposure": EXPOSURE,
            "proven_by": [], "record_body_status": None,
            "note": "a Teams chat attachment an eval case card rests on: read through its "
                    "content URL, served behind the chat message's gate; the pin is the "
                    "named advisory record's",
            "advisory": "advisory:" + PurePosixPath(advisory["path"]).stem[:12]})
    return advisories, files, gaps


def _card_rendered(index: int, case: str, found: Any,
                   records: Mapping[tuple[str, str], bytes], bound: int
                   ) -> tuple[dict[str, Any], dict[str, Any]]:
    """(row, advisory header) of a ``rendered_artifact`` card source: a ``pinned``
    ``derivative`` whose bytes are the complete rendered text (``source_text``) of the
    advisory record it names, read by ``read`` from that record at its commit
    (``repository_ref``, the local Git object store). Proven only when the record is a
    ``plx-advisory/v1`` record of an https source whose ``source_revision`` is
    ``rendered-text-sha256:<the entry's SHA-256>``, whose ``source_text`` UTF-8 bytes have
    that SHA-256 and the entry's size, and whose ``captured_source`` fact states exactly
    that many rendered characters (the capture is complete); anything else fails
    closed."""
    advisory = found.get("advisory") if isinstance(found, Mapping) else None
    if not isinstance(found, Mapping) or set(found) != _CARD_RENDERED_KEYS:
        raise ValueError(f"card_source_invalid:{index}")
    if (not isinstance(advisory, Mapping) or set(advisory) != {"commit", "path"}
            or not re.fullmatch(r"[0-9a-f]{40}", str(advisory["commit"]))
            or not isinstance(advisory["path"], str)
            or not _REPO_PATH.fullmatch(advisory["path"])
            or ".." in advisory["path"].split("/")
            or not isinstance(found["repository"], str)
            or not _REPOSITORY_NAME.fullmatch(found["repository"])
            or not _plain_name(found["name"])
            or not isinstance(found["owner"], str) or not found["owner"].strip()
            or type(found["bytes"]) is not int or found["bytes"] < 0
            or not _HEX64.fullmatch(str(found["sha256"]))):
        raise ValueError(f"card_rendered_invalid:{index}")
    if found["bytes"] > bound:
        raise ValueError(f"card_rendered_oversize:{index}")
    record_raw = records.get((advisory["commit"], advisory["path"]))
    if record_raw is None:
        raise ValueError(f"card_advisory_missing:{index}")
    try:
        record = json.loads(record_raw)
    except ValueError:
        raise ValueError(f"card_advisory_invalid:{index}") from None
    if not isinstance(record, Mapping):
        raise ValueError(f"card_advisory_invalid:{index}")
    text = record.get("source_text")
    data = text.encode("utf-8") if isinstance(text, str) else None
    facts = record.get("facts") if isinstance(record.get("facts"), list) else []
    counted = [f.get("rendered_characters") for f in facts
               if isinstance(f, Mapping) and f.get("type") == "captured_source"]
    if (record.get("schema") != ADVISORY_SCHEMA or data is None
            or not isinstance(record.get("source_id"), str)
            or not record["source_id"].startswith("https://")
            or record.get("source_revision") != f"rendered-text-sha256:{found['sha256']}"
            or sha256(data).hexdigest() != found["sha256"] or len(data) != found["bytes"]
            or counted != [len(text)]):
        raise ValueError(f"card_rendered_unproven:{index}")
    header = _advisory(record, record_raw, advisory["commit"], advisory["path"], "card")
    ref = (f"https://github.com/{found['repository']}/blob/{advisory['commit']}/"
           f"{advisory['path']}")
    return {
        "id": _id("co", f"card:{case}:{record['source_id']}"), "status": "pinned",
        "origin": "card", "card_index": index, "owner": found["owner"], "case": case,
        "case_ids": [case], "kind": RECORD_KINDS[CARD_RENDERED_KIND],
        "record_kind": CARD_RENDERED_KIND, "parent": None, "alias_of": None,
        "linked_from": None, "name": found["name"], "member_path": None,
        "mime_part_index": None,
        "source": {"url": None, "teams_message_url": None, "teams_message_id": None,
                   "teams_attachment_id": None, "drive_id": None, "item_id": None,
                   "repository_ref": ref},
        "revision": {"version_id": None, "etag": None, "modified_at": None,
                     "submitted_at": None, "quick_xor_hash": None, "versions": []},
        "expected": {"bytes": found["bytes"], "sha256": found["sha256"]},
        "mime_type": None, "detected_media_type": None, "exposure": EXPOSURE,
        "proven_by": [], "record_body_status": None,
        "note": "Full rendered text verified against the named advisory record's "
                "rendered-text SHA-256; not the native artifact bytes.",
        "advisory": "advisory:" + PurePosixPath(advisory["path"]).stem[:12]}, header


def _card_records(raw: bytes) -> dict[tuple[str, str], bytes]:
    """The advisory records a card-sources input cites, from the local Git object store
    only (``git cat-file``, no fetch); one missing is left out (``_card_rows`` fails)."""
    try:
        entries = json.loads(raw).get("entries") or []
    except (ValueError, AttributeError):
        return {}
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
           "HOME": os.environ.get("HOME", "/"), "GIT_NO_LAZY_FETCH": "1",
           "GIT_TERMINAL_PROMPT": "0"}
    records: dict[tuple[str, str], bytes] = {}
    for entry in entries if isinstance(entries, list) else []:
        found = (entry.get("chat_attachment") or entry.get("rendered_artifact")
                 if isinstance(entry, Mapping) else None)
        advisory = found.get("advisory") if isinstance(found, Mapping) else None
        if not isinstance(advisory, Mapping):
            continue
        commit, path = advisory.get("commit"), advisory.get("path")
        if not (isinstance(commit, str) and re.fullmatch(r"[0-9a-f]{40}", commit)
                and isinstance(path, str) and _REPO_PATH.fullmatch(path)
                and ".." not in path.split("/")):
            continue
        done = subprocess.run(["git", "-C", str(REPOSITORY_ROOT), "cat-file", "blob",
                               f"{commit}:{path}"], capture_output=True, timeout=60,
                              check=False, env=env)
        if done.returncode == 0:
            records[(commit, path)] = done.stdout
    return records


def _counts(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The registry header's ``counts`` of its rows."""
    files_rows = [r for r in rows if r["status"] in FILE_STATUSES]
    return {"rows": len(rows),
            "file_records": sum(r["origin"] in ("baseline", "delta")
                                and "record_index" in r and r["status"] == "pinned"
                                for r in rows),
            **{status: sum(r["status"] == status for r in rows) for status in ROW_STATUSES},
            "aliases": sum(bool(r.get("alias_of")) for r in rows),
            "captures": sum(r["origin"] == "capture" and not r["parent"] for r in rows),
            "manifest_rows": sum(r["origin"] == "manifest" for r in rows),
            "payloads": len({r["expected"]["sha256"] for r in rows
                             if r["status"] == "pinned"}),
            "by_kind": {kind: sum(r["kind"] == kind for r in files_rows) for kind in KINDS},
            "cases": sorted({c for r in files_rows for c in r["case_ids"]})}


def build_registry(record_bytes: bytes, *, advisory_commit: str, advisory_path: str,
                   delta_bytes: bytes | None = None, delta_commit: str | None = None,
                   delta_path: str | None = None, proven: Mapping[str, str] | None = None,
                   watch_links: Sequence[str] = (),
                   watch_drives: Sequence[Sequence[str]] = (),
                   captures: Sequence[tuple[str, str, bytes, str, str]] = (),
                   receipts: Sequence[Mapping[str, Any]] = (),
                   manifests: Sequence[tuple[str, bytes, str, str]] = (),
                   caveats: Sequence[bytes] = (),
                   crosswalks: Sequence[tuple[str, bytes]] = (),
                   card_sources: bytes | None = None,
                   card_records: Mapping[tuple[str, str], bytes] | None = None,
                   command: str | None = None) -> dict[str, Any]:
    """The registry rows of the source-pin records (module docstring). ``captures``:
    ``(case id, gap id, record bytes, commit, path)`` of each capture record (one row each,
    bound to that case); ``receipts``: full-read receipts that pin unpinned captures;
    ``manifests``: ``(case id, record bytes, commit, path)`` of each manifest record (one
    row per listed file, attachment and zip member, bound to that case); ``caveats``:
    owner caveats kept verbatim on the advisory they name; ``crosswalks``: ``(repository
    path, bytes)`` of each eval card file whose sources are mapped to registry rows;
    ``card_sources``: the card-sources input (``_card_rows``), whose chat attachments are
    checked against ``card_records`` ((commit, path) -> advisory record bytes)."""
    record = json.loads(record_bytes)
    inventory = record.get("source_inventory") if isinstance(record, Mapping) else None
    advisories = [_advisory(record, record_bytes, advisory_commit, advisory_path, "baseline")]
    if not isinstance(inventory, Mapping):
        raise ValueError("advisory_record_invalid:baseline")
    files, gaps = inventory.get("files"), inventory.get("gaps") or []
    if not isinstance(files, list) or not files or not isinstance(gaps, list):
        raise ValueError("advisory_inventory_invalid")
    declared = (inventory.get("counts") or {}).get("all_manifest_entries")
    if declared is not None and declared != len(files):
        raise ValueError("advisory_inventory_count_mismatch")
    delta: Mapping[str, Any] = {}
    if delta_bytes is not None:
        delta = json.loads(delta_bytes)
        advisories.append(_advisory(delta, delta_bytes, delta_commit or "", delta_path or "",
                                    "delta"))
    pins = delta.get("delta_source_pins") or []
    total = (delta.get("counts") or {}).get("all_file_records")
    if total is not None and total != len(files) + len(pins):
        raise ValueError("advisory_delta_count_mismatch")
    proven = proven or {}
    paths: dict[str, list[str]] = {}  # an inventory path -> its rows (never stored)
    rows: list[dict[str, Any]] = []
    for index, entry in enumerate(files):
        if not isinstance(entry, Mapping) or not isinstance(entry.get("relative_path"), str):
            raise ValueError(f"advisory_entry_path_invalid:baseline:{index}")
        rows.append(_file_row(entry, index, _id("co", entry["relative_path"]), "baseline",
                              proven))
        paths.setdefault(entry["relative_path"], []).append(rows[-1]["id"])
        historical = entry.get("historical_reference_version")
        if isinstance(historical, Mapping):
            version = historical.get("version_id")
            if not isinstance(version, str) or version == entry.get("version_id"):
                raise ValueError(f"advisory_historical_revision_invalid:{index}")
            row = json.loads(json.dumps(rows[-1]))
            row.update(id=_id("co", f"{entry['relative_path']}@{version}"), status="unfetched",
                       unfetched_reason="historical_revision", revision_of=rows[-1]["id"],
                       proven_by=[], record_body_status=None,
                       note="historical revision named by the record; bytes not retrieved")
            row["revision"] = {"version_id": version, "etag": None,
                               "modified_at": historical.get("modified_at"),
                               "submitted_at": None, "quick_xor_hash": None, "versions": []}
            row["expected"] = {"bytes": historical.get("size_bytes"),
                               "sha256": historical.get("sha256")}
            rows.append(row)
    by_sha: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        if row["status"] == "pinned":
            by_sha.setdefault(row["expected"]["sha256"], []).append(row)
    for index, entry in enumerate(pins):
        if not isinstance(entry, Mapping) or not isinstance(entry.get("relative_path"), str):
            raise ValueError(f"advisory_entry_path_invalid:delta:{index}")
        key = f"delta:{entry['relative_path']}|{entry.get('drive_id')}/{entry.get('item_id')}"
        row = _file_row(entry, index, _id("co", key), "delta", proven)
        if entry.get("kind") in ALIAS_KINDS:
            targets = by_sha.get(entry["sha256"]) or []
            if len(targets) != 1:
                raise ValueError(f"advisory_alias_unresolved:{index}")
            row["alias_of"] = targets[0]["id"]
        rows.append(row)
        paths.setdefault(entry["relative_path"], []).append(row["id"])
    rows.extend(_mail_rows(delta.get("historical_receiving_source_pins") or {}))
    # parents (archive members, EML attachments) by the container's bytes
    for row in rows:
        if row["kind"] in ("archive_member", "attachment") and row["origin"] == "baseline":
            parent_sha = files[row["record_index"]].get("parent_sha256")
            found = by_sha.get(parent_sha) or []
            if len(found) != 1:
                raise ValueError(f"advisory_entry_parent_unresolved:{row['record_index']}")
            row["parent"] = found[0]["id"]
    names: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        if row["status"] == "pinned" and not row["parent"] and row["origin"] == "baseline":
            names.setdefault(row["name"], []).append(row)
    for row in rows:
        if row["linked_from"] is not None:
            target = [r for r in names.get(row["linked_from"], []) if not r["linked_from"]]
            if len(target) != 1:
                raise ValueError(f"advisory_entry_link_unresolved:{row['record_index']}")
            row["linked_from"] = target[0]["id"]
    register = delta.get("case_register")
    own: dict[str, set[str]] = {}
    for case in register or []:
        case_id = case.get("case_id") if isinstance(case, Mapping) else None
        if not isinstance(case_id, str) or not _TOKEN.fullmatch(case_id):
            raise ValueError("advisory_case_register_invalid")
        for path in case.get("file_paths") or []:
            for row_id in paths.get(path, []):
                own.setdefault(row_id, set()).add(case_id)
    by_id = {row["id"]: row for row in rows}

    def cases(row: dict[str, Any]) -> list[str]:
        """Inherited first (container, linking map, revision), then the row's own."""
        if "_cases" not in row:
            inherited: list[str] = []
            for origin in (row["parent"], row["linked_from"], row.get("revision_of"),
                           row["alias_of"]):
                if origin:
                    inherited += [c for c in cases(by_id[origin]) if c not in inherited]
            row["_cases"] = inherited + sorted(own.get(row["id"], set()) - set(inherited))
        return row["_cases"]

    for row in rows:
        row["case_ids"] = cases(row)
    for row in rows:
        del row["_cases"]
        if register is not None:
            row["case"] = row["case_ids"][0] if row["case_ids"] else None
        elif not row["case"]:
            origin = row["parent"] or row["linked_from"] or row.get("revision_of")
            chain = row
            while origin:
                chain = by_id[origin]
                origin = chain["parent"] or chain["linked_from"] or chain.get("revision_of")
            row["case"] = chain["case"] or _case_of(chain["name"])
    known = {(r["source"].get("drive_id"), r["source"].get("item_id")) for r in rows
             if r["source"].get("item_id")}
    for index, (case, gap, raw, commit, path) in enumerate(captures):
        row = _capture_row(case, gap, raw, commit, path, index)
        if (row["source"]["drive_id"], row["source"]["item_id"]) in known:
            raise ValueError(f"capture_duplicates_row:{index}")
        known.add((row["source"]["drive_id"], row["source"]["item_id"]))
        rows.append(row)
    _pin_receipts(rows, receipts)
    for index, (case, raw, commit, path) in enumerate(manifests):
        head, added = _manifest_rows(case, raw, commit, path, index, watch_drives)
        advisories.append(head)
        rows.extend(added)
    card_gaps: list[dict[str, Any]] = []
    if card_sources is not None:
        heads, card_files, card_gaps = _card_rows(card_sources, card_records or {})
        pinned = {r["expected"]["sha256"] for r in rows if r["status"] == "pinned"}
        for row in card_files:
            if row["expected"]["sha256"] in pinned:  # name that row on the card instead
                raise ValueError(f"card_attachment_duplicates_row:{row['card_index']}")
            pinned.add(row["expected"]["sha256"])
        advisories.extend(heads)
        rows.extend(card_files)
    _same_bytes(rows)
    _caveats(advisories, caveats)
    rows.extend(_gap_rows(gaps, delta.get("open_gaps") if delta else None))
    rows.extend(card_gaps)
    if len({row["id"] for row in rows}) != len(rows):
        raise ValueError("registry_ids_not_unique")
    watch = _watch(watch_links, watch_drives, rows)
    return {
        "schema": SCHEMA,
        "note": "Nonsecret registry of every stakeholder original in the source-pin "
                "records and of those eval cards name (origin card, from the "
                "--card-sources input) (generated; do not edit by hand). Rows hold identity and expected "
                "bytes/SHA-256 only: no file body, temporary download URL, local path or "
                "secret. Bodies are read into the credentialed capture store "
                "(PLX_WORKBENCH_STATE_DIR/case-originals) by python3 -m "
                "scripts.case_originals read, never into Git.",
        "generated_by": command,
        "advisories": advisories,
        "exposure": EXPOSURE,
        "counts": _counts(rows),
        "watch_folders": watch,
        **({"crosswalk": _crosswalk(crosswalks, rows, watch)} if crosswalks else {}),
        "rows": rows,
    }


def load_registry(path: str | Path) -> tuple[dict[str, Any], str]:
    """The registry and its version (SHA-256 of the file); fails closed on any unknown."""
    raw = Path(path).read_bytes()
    registry = json.loads(raw)
    if not isinstance(registry, Mapping) or registry.get("schema") != SCHEMA:
        raise ValueError("registry_schema")
    rows = registry.get("rows")
    if not isinstance(rows, list) or not rows:
        raise ValueError("registry_rows")
    ids = {row.get("id") for row in rows if isinstance(row, Mapping)}
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, Mapping) or row.get("status") not in ROW_STATUSES \
                or not _TOKEN.fullmatch(str(row.get("id"))) or row["id"] in seen:
            raise ValueError("registry_row_invalid")
        seen.add(row["id"])
        if row["status"] in ("gap", "superseded"):
            continue
        if row.get("kind") not in KINDS or not isinstance(row.get("source"), Mapping):
            raise ValueError("registry_row_invalid")
        expected = row.get("expected") if isinstance(row.get("expected"), Mapping) else {}
        if row["status"] == "pinned" and not (
                _HEX64.fullmatch(str(expected.get("sha256")))
                and type(expected.get("bytes")) is int):
            raise ValueError("registry_row_pin_invalid")
        if row["status"] in ("unpinned", "oversize") and not (
                type(expected.get("bytes")) is int and (row.get("parent") or (
                    row["source"].get("drive_id") and row["source"].get("item_id")))):
            raise ValueError("registry_row_capture_invalid")
        if row["status"] == "oversize" and not (
                isinstance(row.get("oversize"), Mapping)
                and type(row["oversize"].get("bound")) is int):
            raise ValueError("registry_row_oversize_invalid")
        if row.get("parent") is not None and row["parent"] not in ids:
            raise ValueError("registry_row_parent_unknown")
    for folder in registry.get("watch_folders") or []:
        if not isinstance(folder, Mapping) or not _TOKEN.fullmatch(str(folder.get("id"))) \
                or not isinstance(folder.get("folder_url"), str):
            raise ValueError("registry_watch_folder_invalid")
    return dict(registry), sha256(raw).hexdigest()


# ------------------------------------------------------------------ read

def _budget_cut() -> Exception:
    """A download refused because the run's budget is spent (``_failure``: ``budget_cut``)."""
    from integrations.live_source_guard import SourceUnavailable

    exc = SourceUnavailable("budget_cut")
    exc.budget_cut = True  # type: ignore[attr-defined]
    return exc


class _Recorder:
    """The Graph ``SourceApi`` with the last HTTP failure kept, so an entry the reader
    reports ``not_visible`` (Graph's 404 included) still names its status and code.
    With ``remaining`` (seconds left of the run's budget) no download starts once it is
    spent: the read raises a budget cut (``_budget_cut``) and is counted in ``budget_cuts``."""

    def __init__(self, api: Any) -> None:
        self._api, self.failure = api, None
        self.remaining: Any = None
        self.budget_cuts = 0

    def __getattr__(self, name: str) -> Any:
        from integrations.live_source_guard import SourceUnavailable, failure_diagnostics

        attr = getattr(self._api, name)
        if name not in ("get_json", "get_content", "get_pages"):
            return attr

        def call(*args: Any, **kwargs: Any) -> Any:
            if name == "get_content" and self.remaining is not None \
                    and self.remaining() <= 0:
                self.budget_cuts += 1
                raise _budget_cut()
            try:
                return attr(*args, **kwargs)
            except SourceUnavailable as exc:
                self.failure = failure_diagnostics(exc)
                raise
        return call


def _failure(exc: BaseException, recorded: Mapping[str, Any] | None) -> tuple[str, dict]:
    """``(body status, diagnosis)`` of a failed read: HTTP status and error-code token
    from Graph where it answered (401/403 ``denied``, 404 ``gone``: the item or link no
    longer exists); a local refusal by its fixed detail."""
    from integrations.live_source_guard import (DownloadIncomplete, NotVisible,
                                                ReadOnlyGuardError, failure_diagnostics)
    from integrations.people_exclusion import ExclusionUnavailable

    if isinstance(exc, ReadOnlyGuardError):
        return "denied", {"error_class": "ReadOnlyGuardError", "http_status": None,
                          "error_code": None, "detail": "read_only_guard"}
    if getattr(exc, "budget_cut", False):
        return "unavailable", {"error_class": "BudgetCut", "http_status": None,
                               "error_code": None, "detail": "budget_cut"}
    if isinstance(exc, ExclusionUnavailable):
        # The people-exclusion guard could not decide, so it refused the read before any
        # request for it: unavailable by the guard's code, never an HTTP verdict on the item.
        return "unavailable", {"error_class": "ExclusionUnavailable", "http_status": None,
                               "error_code": exc.code, "detail": exc.code}
    if isinstance(exc, NotVisible):
        if not recorded:
            return "denied", {"error_class": "NotVisible", "http_status": None,
                              "error_code": None, "detail": "not_visible"}
        diagnosis = {**recorded, "error_class": "NotVisible"}
    else:
        diagnosis = failure_diagnostics(exc)
        if diagnosis["detail"] is None and type(exc) is ValueError:
            diagnosis["detail"] = "invalid_pin"  # refused before any request was sent
    if isinstance(exc, DownloadIncomplete):
        return "partial", diagnosis
    status = diagnosis.get("http_status")
    return ("denied" if status in (401, 403) else "gone" if status == 404
            else "unavailable"), diagnosis


def _extraction(parts: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    from operating.use_case_parts import EXTRACTED, SKIPPED, UNREAD

    counts = {state: sum(p["state"] == state for p in parts)
              for state in (EXTRACTED, UNREAD, SKIPPED)}
    status = ("complete" if parts and counts[EXTRACTED] == len(parts)
              else "partial" if counts[EXTRACTED] else "unread")
    return {"status": status, "parts": counts,
            "methods": sorted({p["method"] for p in parts
                               if p["state"] == EXTRACTED and p.get("method")}),
            "unread_reasons": sorted({p["reason"] for p in parts if p["state"] == UNREAD}
                                     | {f"cap:{p['cap']}" for p in parts
                                        if p["state"] == SKIPPED})}


OCR_METHOD = "tesseract-ocr"
MACHINE_READ_LEAD = "machine_read_lead"


def render_findings(receipts: Sequence[Mapping[str, Any]], store: Any
                    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The case finding each extracted original renders as, and each OCR-derived value:
    ``cited_fact`` only when its citation (the stored bytes, re-hashed, with the stored
    text) resolves and the bytes matched, else ``model_output_interpretation``
    (operating.cases); an OCR-derived value is a machine-read lead, never verified."""
    from operating.cases import FACT_LABEL, INTERPRETATION_LABEL

    findings, ocr_values = [], []
    for receipt in receipts:
        extraction = receipt.get("extraction") or {}
        if not extraction.get("text_ref"):
            continue
        resolves = receipt.get("match") is True and store.intact(
            receipt.get("sha256"), receipt.get("stored"), extraction["text_ref"])
        findings.append({"label": FACT_LABEL if resolves else INTERPRETATION_LABEL,
                         "citation_resolves": resolves})
        if OCR_METHOD in (extraction.get("methods") or ()):
            ocr_values.append({"claim": MACHINE_READ_LEAD})
    return findings, ocr_values


def audit_findings(findings: Sequence[Mapping[str, Any]],
                   ocr_values: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Counts only: rendered findings by label, ``mislabelled`` (no known label, or a
    ``cited_fact`` whose citation does not resolve) and ``verified_ocr`` (an OCR-derived
    value claimed as anything but a machine-read lead)."""
    from operating.cases import FACT_LABEL, INTERPRETATION_LABEL

    labels = (FACT_LABEL, INTERPRETATION_LABEL)
    return {
        "case_findings": {
            "rendered": len(findings),
            FACT_LABEL: sum(f.get("label") == FACT_LABEL for f in findings),
            INTERPRETATION_LABEL: sum(f.get("label") == INTERPRETATION_LABEL
                                      for f in findings),
            "mislabelled": sum(f.get("label") not in labels or (
                f.get("label") == FACT_LABEL and f.get("citation_resolves") is not True)
                for f in findings)},
        "ocr_values": {
            "derived": len(ocr_values),
            MACHINE_READ_LEAD: sum(v.get("claim") == MACHINE_READ_LEAD for v in ocr_values),
            "verified_ocr": sum(v.get("claim") != MACHINE_READ_LEAD for v in ocr_values)}}


def item_isolation(entries: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """ING-10, from the per-item outcomes in report order: an item that failed (a body
    not read, or read but extraction ``unread``) is ``typed`` when it carries a status
    token and a diagnosis, ``untyped`` otherwise; a failure must not stop the run, so
    ``stalled`` counts a failure followed by items none of which was attempted (every
    later one ``budget_cut``). ``outcome`` is ``not_exercised`` with no failing item,
    never a pass. Counts and enum tokens only."""
    def attempted(e: Mapping[str, Any]) -> bool:
        return e.get("detail") != "budget_cut"

    def failed(e: Mapping[str, Any]) -> bool:
        return attempted(e) and (e.get("status") not in READ_STATUSES
                                 or e.get("extraction") == "unread")

    def typed(e: Mapping[str, Any]) -> bool:
        known = (str(e.get("status") or "").removeprefix("discovered_")
                 .removeprefix("revision_") in BODY_STATUSES)
        return known and bool(e.get("http_status") or e.get("error_code") or e.get("detail")
                              or e.get("extraction") in EXTRACTION_STATUSES)

    bad = [i for i, e in enumerate(entries) if failed(e)]
    later = list(entries[bad[0] + 1:]) if bad else []
    later_attempted = sum(attempted(e) for e in later)
    later_read = sum(attempted(e) and not failed(e) and e.get("status") in READ_STATUSES
                     for e in later)
    untyped = sum(not typed(entries[i]) for i in bad)
    stalled = 1 if bad and later and not later_attempted else 0
    return {"items": len(entries), "failed": len(bad), "typed": len(bad) - untyped,
            "untyped": untyped, "later_attempted": later_attempted, "later_read": later_read,
            "stalled": stalled,
            "outcome": ("not_exercised" if not bad
                        else "isolated" if not untyped and not stalled else "broken")}


_MEDIA_TYPES = {
    "pdf": "application/pdf", "png": "image/png", "jpg": "image/jpeg",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "vsdx": "application/vnd.ms-visio.drawing", "zip": "application/zip",
    "msg": "application/vnd.ms-outlook", "eml": "message/rfc822", "html": "text/html",
    "md": "text/markdown", "txt": "text/plain"}


def _write(target: Path, data: bytes) -> None:
    temporary = target.with_name(target.name + ".tmp")
    descriptor = os.open(temporary, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, target)


class _Store:
    """``<state>/case-originals``: originals by SHA-256, extracted text, receipts."""

    def __init__(self, state_dir: str | Path, registry_version: str) -> None:
        self.state = Path(state_dir)
        self.root = self.state / STORE_DIR
        for sub in ("extractions", f"receipts/{registry_version}", DISCOVERED_RECEIPTS):
            (self.root / sub).mkdir(parents=True, exist_ok=True, mode=0o700)
        self.receipts = self.root / "receipts" / registry_version
        self.discovered = self.root / DISCOVERED_RECEIPTS

    def pin(self, content: bytes) -> str:
        digest = sha256(content).hexdigest()
        target = self.root / digest
        if not target.exists() or sha256(target.read_bytes()).hexdigest() != digest:
            _write(target, content)
        return f"{STORE_DIR}/{digest}"

    def write_text(self, digest: str, text: str) -> str:
        _write(self.root / "extractions" / f"{digest}.txt", text.encode("utf-8"))
        return f"{STORE_DIR}/extractions/{digest}.txt"

    def receipt(self, receipt: Mapping[str, Any]) -> None:
        folder = self.discovered if receipt.get("status") == "discovered" else self.receipts
        _write(folder / f"{receipt['entry_id']}.json",
               json.dumps(receipt, sort_keys=True, ensure_ascii=False).encode("utf-8"))

    def prior(self, entry_id: str) -> dict[str, Any] | None:
        """The latest receipt of discovered file ``entry_id`` from an earlier run, whatever
        registry version it was read under, or None."""
        try:
            found = json.loads((self.discovered / f"{entry_id}.json").read_bytes())
        except (OSError, ValueError):
            return None
        return found if isinstance(found, dict) and found.get("entry_id") == entry_id else None

    def intact(self, digest: Any, stored: Any, text_ref: Any) -> bool:
        """The pinned original ``digest`` is stored at ``stored`` and re-hashes, and its
        extracted text (``text_ref``, if any) is stored too."""
        if not isinstance(digest, str) or stored != f"{STORE_DIR}/{digest}":
            return False
        if text_ref is not None and text_ref != f"{STORE_DIR}/extractions/{digest}.txt":
            return False
        try:
            if sha256((self.root / digest).read_bytes()).hexdigest() != digest:
                return False
        except OSError:
            return False
        return text_ref is None or (self.state / text_ref).is_file()


class _PathMiss(Exception):
    """A drive path that holds no file of the row's identity (``_Run._at_path``)."""

    def __init__(self, status: str, detail: str) -> None:
        super().__init__(detail)
        self.status, self.detail = status, detail


class _Run:
    """One read of the whole registry (``read``)."""

    def __init__(self, registry: Mapping[str, Any], version: str, state_dir: str | Path,
                 environ: Mapping[str, str], reader: Mapping[str, Any], sender: Any,
                 clock: Any, ocr: Any, retry: Any = None) -> None:
        from integrations.graph_read import build_graph
        from integrations.live_source_guard import LiveSourceAudience, ReadOnlyGuardError
        from ontology.core import Actor

        self.registry, self.version, self.reader = registry, version, reader
        self.clock = clock or (lambda: datetime.now(_UTC))
        self.ocr = ocr
        self.remaining: Any = None  # seconds left of the run's budget (``read``)
        api, self.transport = build_graph(environ, sender=sender, retry=retry)
        if not self.transport.download_hosts:
            raise ReadOnlyGuardError("no SharePoint host configured; originals refused")
        self.api = _Recorder(api)
        principal = f"reader:case-originals:{reader['role']}"
        self.audience = LiveSourceAudience({"sharepoint:*": [principal],
                                            "mail:*": [principal]}, tenant="plx")
        self.mailbox = (environ.get(MAILBOX_ENV) or "").strip() or None
        self._mail: Any = None
        self.actor = Actor("plx", frozenset({principal}), "restricted")
        self.store = _Store(state_dir, version)
        self.rows = [r for r in registry["rows"] if r["status"] in FILE_STATUSES]
        self.by_id = {r["id"]: r for r in self.rows}
        self.children: dict[str, list[Mapping[str, Any]]] = {}
        for row in self.rows:
            if row.get("parent"):
                self.children.setdefault(row["parent"], []).append(row)
        self.results: dict[str, dict[str, Any]] = {}  # registered entries, by id
        self.extra: list[dict[str, Any]] = []  # revision and discovered receipts, in order
        self.item_of: dict[str, tuple[str, str]] = {
            r["id"]: (r["source"]["drive_id"], r["source"]["item_id"]) for r in self.rows
            if r["source"].get("drive_id") and r["source"].get("item_id")}
        self.known_items: set[tuple[str, str]] = set(self.item_of.values())
        self.listed_in: dict[tuple[str, str], str] = {}  # a registered item -> its folder
        self.discovered_ids: set[str] = set()
        self.discovered_unreached = 0
        self.discovered_ocr_cut = 0  # discovered files read with an image the budget cut
        self.discovered_new = 0  # discovered files downloaded and read in full this run
        # (folder row, folder, item, entry id) listed by ``watch``, read by ``read_discovered``
        self.deferred: list[tuple[dict[str, Any], Mapping[str, Any], Mapping[str, Any], str]] = []
        # Reuse an earlier run's full read of a discovered file listed with the eTag it
        # read (``_unchanged``); how many were reused.
        self.reuse_unchanged = False
        self.discovered_reused = 0
        self.ocr_cut = 0  # images left unread because the budget was spent
        self._readers: dict[str, Any] = {}
        self._listings: dict[tuple[str, str], tuple[Any, Any]] = {}  # (drive, folder path)

    def _drive(self, drive_id: str) -> Any:
        from integrations.graph_read import SharePointReader

        if drive_id not in self._readers:
            self._readers[drive_id] = SharePointReader(self.api, self.audience, drive_id,
                                                       transport=self.transport)
        return self._readers[drive_id]

    def _mail_reader(self) -> Any:
        from integrations.graph_read import GraphMailReader

        if self._mail is None:
            self._mail = GraphMailReader(self.api, self.audience)
        return self._mail

    # -- receipts
    def _receipt(self, row: Mapping[str, Any], **fields: Any) -> dict[str, Any]:
        expected = row.get("expected") or {}
        source = row.get("source") or {}
        receipt = {
            "schema": RECEIPT_SCHEMA, "registry_version": self.version,
            "entry_id": row["id"], "status": "registered", "name": row.get("name"),
            "owner": row.get("owner"),
            "case": row.get("case"), "case_ids": list(row.get("case_ids") or []),
            "kind": row.get("kind"), "parent": row.get("parent"),
            "alias_of": row.get("alias_of"), "revision_of": None, "current_revision": None,
            "watch_folder": None,
            "canonical_source": {"url": source.get("url"),
                                 "teams_message_url": source.get("teams_message_url"),
                                 "drive_id": source.get("drive_id"),
                                 "item_id": source.get("item_id"),
                                 "drive_path": source.get("drive_path"),
                                 "repository_ref": source.get("repository_ref"),
                                 "member_path": row.get("member_path"),
                                 "attachment_name": row.get("name")
                                 if row.get("kind") == "attachment" else None},
            "provider_revision": {"version_id": (row.get("revision") or {}).get("version_id"),
                                  "etag": (row.get("revision") or {}).get("etag"),
                                  "etag_read": None},
            "expected": {"bytes": expected.get("bytes"), "sha256": expected.get("sha256")},
            "bytes": None, "sha256": None, "match": None, "match_basis": None,
            "body_status": None, "http_status": None, "error_code": None,
            "error_class": None, "detail": None,
            "read_via": ("drive_item" if source.get("item_id") else "drive_path"
                         if source.get("drive_path") else "shared_link"
                         if source.get("url") else "container" if row.get("parent")
                         else "repository" if source.get("repository_ref") else None),
            "provider_mime_type": row.get("mime_type"),
            "registered_media_type": row.get("detected_media_type"),
            "detected_type": None, "detected_media_type": None, "type_warnings": [],
            "stored": None, "member_manifest": None,
            "extraction": {"status": "not_run", "methods": [], "parts": {},
                           "unread_reasons": [], "text_ref": None},
            "proven_by": list(row.get("proven_by") or []),
            "read_at": self.clock().astimezone(_UTC).isoformat(), "reader": dict(self.reader)}
        for key, value in fields.items():
            if isinstance(value, Mapping) and isinstance(receipt.get(key), dict):
                receipt[key].update(value)
            else:
                receipt[key] = value
        return receipt

    def _done(self, receipt: dict[str, Any]) -> None:
        if receipt["body_status"] not in BODY_STATUSES:
            raise ValueError("body_status")
        self.store.receipt(receipt)
        if receipt["status"] == "registered":
            self.results[receipt["entry_id"]] = receipt
        else:
            self.extra.append(receipt)

    def _fail_tree(self, row: Mapping[str, Any], status: str, diagnosis: Mapping[str, Any],
                   **fields: Any) -> None:
        """``row`` and every member under it: a member of a denied, gone or unrouted
        container is so itself; any other container failure leaves it unavailable (a
        container cut by the budget: its members are ``budget_cut`` too). A routed pin
        under a routed pin (a mail attachment) shares its parent's reason."""
        self._done(self._receipt(row, body_status=status, **diagnosis, **fields))
        inherited = status if status in ("denied", "gone", "no_route") else "unavailable"
        detail = ("budget_cut" if diagnosis.get("detail") == "budget_cut"
                  else f"container_{status}")
        for child in self.children.get(row["id"], []):
            if child["status"] == "unfetched" and _routed(child):
                self._fail_tree(child, inherited, diagnosis)
                continue
            own = child.get("unfetched_reason") if child["status"] == "unfetched" else None
            self._fail_tree(child, "no_route" if own in NO_ROUTE_REASONS else inherited,
                            {**diagnosis, "detail": own or detail})

    def _ingest(self, content: bytes, name: str | None
                ) -> tuple[dict[str, Any], Mapping[str, Any] | None]:
        """Type by bytes; the bytes and their extraction into the store. A row an earlier
        capture proved is stored too: the host serves an original only from this store,
        and that capture's bytes are held nowhere it reads."""
        from operating.use_case_intake import detect_type
        from operating.use_case_parts import account

        name = name or "original"
        detected, warnings = detect_type(content, name)
        seen: dict[str, Any] = {"detected_type": detected,
                                "detected_media_type": _MEDIA_TYPES.get(detected),
                                "type_warnings": warnings}
        seen["stored"] = self.store.pin(content)
        accounting = account(content, name, pin=self.store.pin, ocr=self.ocr,
                             max_part_bytes=MAX_PART_BYTES, remaining=self.remaining)
        text = accounting["text"]
        self.ocr_cut += sum(p.get("cap") == "budget_seconds" for p in accounting["parts"])
        seen["extraction"] = {**_extraction(accounting["parts"]), "text_ref": (
            self.store.write_text(sha256(content).hexdigest(), text) if text else None)}
        return seen, accounting

    @staticmethod
    def _manifest(found: Mapping[str, Any] | None) -> dict[str, Any] | None:
        """Every member of a container read: path, name, bytes, SHA-256 and type by bytes;
        a member over ``MAX_PART_BYTES`` is an ``oversize`` member with its bound, one
        that could not be read ``unread`` with its reason: counted, never dropped."""
        from operating.use_case_intake import detect_type

        if found is None:
            return None
        listed = [{"path": f["path"], "name": f["name"], "bytes": len(f["data"]),
                   "sha256": f["sha256"], "kind": detect_type(f["data"], f["name"])[0],
                   "status": "read"} for f in found["files"]]
        for gap in found["gaps"]:
            over = gap["state"] == "skipped_by_cap"
            listed.append({"path": gap["path"],
                           "name": PurePosixPath(gap["path"].rsplit("!/", 1)[-1]).name,
                           "bytes": gap.get("size"), "sha256": None, "kind": None,
                           "status": "oversize" if over else "unread",
                           "bound": gap.get("limit") if over else None,
                           "reason": gap.get("cap") if over else gap.get("reason")})
        return {"members": listed,
                "counts": {s: sum(m["status"] == s for m in listed) for s in _MANIFEST_STATUSES}}

    # -- one top-level original
    def _fetch(self, row: Mapping[str, Any]) -> tuple[str, Mapping[str, Any]] | None:
        from integrations.shared_link import read_shared_link
        from operating.use_case_intake import MAX_CONTENT_BYTES

        source = row["source"]
        if source.get("drive_id") and source.get("item_id"):
            return "drive_item", self._drive(source["drive_id"]).get_content(
                self.actor, source["item_id"], max_bytes=MAX_CONTENT_BYTES, content_route=True)
        if source.get("drive_id") and source.get("drive_path"):
            item_id = self._at_path(row)
            read = self._drive(source["drive_id"]).get_content(
                self.actor, item_id, max_bytes=MAX_CONTENT_BYTES, content_route=True)
            return "drive_path", read
        if source.get("url"):
            read = read_shared_link(self.api, self.transport, self.audience, self.actor,
                                    source["url"], max_bytes=MAX_CONTENT_BYTES)
            self.known_items.add((read["drive_id"], read["item_id"]))
            self.item_of[row["id"]] = (read["drive_id"], read["item_id"])
            return "shared_link", read
        return None

    def _at_path(self, row: Mapping[str, Any]) -> str:
        """The drive item a row's drive path holds now: its folder listed once per run
        (``SharePointReader.list_folder``, ``graph.drive_path`` then
        ``graph.drive_children``), the file of that name (case-insensitive, as SharePoint
        names are) taken only when its eTag names the row's list item
        (``list_item_unique_id``); any other item at the path is never read as it
        (``_PathMiss``). A name not listed is gone only from a complete listing that
        withheld nothing; a listing the reader's permission filter thinned
        (``withheld_count``, ``provenance_dropped``) cannot tell absent from hidden
        (``path_withheld``)."""
        source = row["source"]
        drive = source["drive_id"]
        folder, _, name = source["drive_path"].rpartition("/")
        key = (drive, folder or "/")
        if key not in self._listings:
            self.api.failure = None
            try:
                self._listings[key] = (self._drive(drive).list_folder(
                    self.actor, folder or "/", max_items=WATCH_MAX_ITEMS), None)
            except Exception as exc:  # replayed for every row in this folder
                self._listings[key] = (exc, self.api.failure)
        listing, failure = self._listings[key]
        if isinstance(listing, BaseException):
            self.api.failure = failure
            raise listing
        found = [i for i in listing["items"] if i.get("kind") == "file"
                 and str(i.get("name")).casefold() == name.casefold()]
        if len(found) != 1:
            done = listing["completeness"]
            if found:
                raise _PathMiss("unavailable", "path_ambiguous")
            if not done.get("complete"):
                raise _PathMiss("unavailable", "listing_incomplete")
            if done.get("withheld_count", 0) or done.get("provenance_dropped", 0):
                raise _PathMiss("unavailable", "path_withheld")
            raise _PathMiss("gone", "path_not_located")
        tag = _ETAG_RE.fullmatch(found[0].get("etag") or "")
        if tag is None:
            raise _PathMiss("unavailable", "etag_unrecognised")
        if tag.group(1).lower() != str(source.get("list_item_unique_id")).lower():
            raise _PathMiss("gone", "path_holds_other_item")
        item = (drive, found[0]["id"])
        self.item_of[row["id"]] = item
        self.known_items.add(item)
        return found[0]["id"]

    def _repository(self, row: Mapping[str, Any]
                    ) -> tuple[bytes | None, str | None, dict[str, Any]]:
        """A derivative held in this repository's history (``repository_ref``, a GitHub
        blob URL at a commit): read from the local object store only (no fetch). The
        named bytes are the blob, or the one text field of that committed record whose
        UTF-8 bytes have the registered SHA-256 (a rendering kept inside a record)."""
        from operating.use_case_intake import MAX_CONTENT_BYTES

        found = _REPOSITORY_REF.fullmatch(row["source"].get("repository_ref") or "")
        if not found or ".." in found.group(4).split("/"):
            return None, None, {"detail": "repository_ref_invalid"}
        commit, path = found.group(3), found.group(4)
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
               "HOME": os.environ.get("HOME", "/"), "GIT_NO_LAZY_FETCH": "1",
               "GIT_TERMINAL_PROMPT": "0"}
        git = ["git", "-C", str(REPOSITORY_ROOT), "cat-file"]
        try:
            size = subprocess.run([*git, "-s", f"{commit}:{path}"], capture_output=True,
                                  timeout=60, check=False, env=env)
            if size.returncode != 0:
                return None, None, {"detail": "repository_object_absent"}
            if int(size.stdout.strip() or b"-1") > MAX_CONTENT_BYTES:
                return None, None, {"detail": "exceeds_cap"}
            done = subprocess.run([*git, "blob", f"{commit}:{path}"], capture_output=True,
                                  timeout=60, check=False, env=env)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return None, None, {"detail": "repository_unreadable"}
        if done.returncode != 0:
            return None, None, {"detail": "repository_object_absent"}
        blob = done.stdout
        expected = row["expected"].get("sha256")
        if sha256(blob).hexdigest() == expected:
            return blob, "repository", {}
        try:
            document = json.loads(blob)
        except ValueError:
            return blob, "repository", {}
        for text in _strings(document):
            data = text.encode("utf-8")
            if sha256(data).hexdigest() == expected:
                return data, "repository_field", {}
        return blob, "repository", {}

    def original(self, row: Mapping[str, Any]) -> None:
        from integrations.live_source_guard import (NotVisible, ReadOnlyGuardError,
                                                    SourceUnavailable)

        if row["status"] == "unfetched":
            route = _routed(row)
            if route == "drive_item_version":
                self._version(row)
                return
            if route == "mail" and not row.get("parent"):
                if self.mailbox:
                    self._mail_message(row)
                else:
                    self._fail_tree(row, "no_route", {"detail": "mailbox_not_configured"})
                return
            reason = row.get("unfetched_reason")
            self._fail_tree(row, "no_route" if reason in NO_ROUTE_REASONS else "unavailable",
                            {"detail": reason or "not_fetchable"})
            return
        source = row["source"]
        if (source.get("repository_ref") and not source.get("url")
                and not source.get("item_id")):
            content, via, diagnosis = self._repository(row)
            if content is None:
                self._fail_tree(row, "unavailable", diagnosis)
            else:
                self._verified(row, content, {"read_via": via})
            return
        self.api.failure = None
        try:
            fetched = self._fetch(row)
        except _PathMiss as miss:
            self._fail_tree(row, miss.status, {"detail": miss.detail})
            return
        except (NotVisible, SourceUnavailable, ReadOnlyGuardError) as exc:
            status, diagnosis = _failure(exc, self.api.failure)
            self._fail_tree(row, status, diagnosis)
            return
        if fetched is None:
            self._fail_tree(row, "unavailable", {"detail": "no_graph_source"})
            return
        via, read = fetched
        record = read["record"]
        seen = {"read_via": via, "provider_revision": {"etag_read": record.get("etag")}}
        if read.get("content_status") != "read":
            self._fail_tree(row, "capped", {"detail": "exceeds_cap"},
                            bytes=record.get("size"), **seen)
            return
        self._verified(row, read["content"], seen)

    def _version(self, row: Mapping[str, Any]) -> None:
        """A historical revision: the named version's bytes through
        ``graph.drive_item_version_content``, compared with the pin (``_verified``)."""
        from integrations.live_source_guard import (NotVisible, ReadOnlyGuardError,
                                                    SourceUnavailable)
        from operating.use_case_intake import MAX_CONTENT_BYTES

        source, version = row["source"], row["revision"]["version_id"]
        seen: dict[str, Any] = {"read_via": "drive_item_version"}
        self.api.failure = None
        try:
            read = self._drive(source["drive_id"]).get_version_content(
                self.actor, source["item_id"], version, max_bytes=MAX_CONTENT_BYTES)
        except (NotVisible, SourceUnavailable, ReadOnlyGuardError, ValueError) as exc:
            status, diagnosis = _failure(exc, self.api.failure)
            self._fail_tree(row, status, diagnosis, **seen)
            return
        seen["provider_revision"] = {"etag_read": read["record"].get("etag")}
        self._verified(row, read["content"], seen)

    def _mail_message(self, row: Mapping[str, Any]) -> None:
        """A historical mail pin in ``MAILBOX_ENV``'s mailbox. The pin is a REST id and the
        reader works in immutable ids, so the message is located by its exact received
        second (one listing GET, oldest first from that second; several in that second: the
        one whose text body hashes to the pin's ``body_sha256``, else ambiguous), then its
        text body is read, stored and extracted: ``read`` (``capped`` past ``MAIL_BODY_CHARS``) with
        ``match_basis`` ``received_at`` (the pin's body hash names no basis, so no byte
        match is claimed). A malformed pin is ``unavailable``, never a crash. Each pinned attachment is found under it by name and its bytes
        are verified like any original (``_verified``); none found, or more than one,
        reads ``gone`` or ``unavailable`` with the reason."""
        from integrations.live_source_guard import (NotVisible, ReadOnlyGuardError,
                                                    SourceUnavailable)

        mail, received = self._mail_reader(), row["source"]["received_at"]
        basis = "received_at"
        seen: dict[str, Any] = {"read_via": "mail"}
        self.api.failure = None
        try:
            listing = mail.list_messages(self.actor, self.mailbox, received_since=received,
                                         page_size=MAIL_LISTING_PAGE, max_pages=1,
                                         max_items=MAIL_LISTING_PAGE, oldest_first=True)
            when = _instant(received)
            found = [item for item in listing["items"]
                     if _instant(item.get("received_at")) == when]
            pinned, bodies = (row["expected"].get("body_sha256"), {})
            if len(found) > 1 and pinned:  # same second: only the pinned body tells them apart
                for item in found:
                    bodies[item["id"]] = mail.get_message(
                        self.actor, self.mailbox, item["id"], include_body=True,
                        max_body_chars=MAIL_BODY_CHARS)
                hashed = [item for item in found if sha256(
                    (bodies[item["id"]].get("body_text") or "").encode("utf-8")
                ).hexdigest() == pinned]
                if len(hashed) == 1:
                    found, basis = hashed, "body_sha256"
            if len(found) != 1:
                self._fail_tree(row, "gone" if not found else "unavailable",
                                {"detail": "message_not_located" if not found
                                 else "message_ambiguous"}, **seen)
                return
            message = bodies.get(found[0]["id"]) or mail.get_message(
                self.actor, self.mailbox, found[0]["id"], include_body=True,
                max_body_chars=MAIL_BODY_CHARS)
            children = self.children.get(row["id"], [])
            listed = mail.list_attachments(self.actor, self.mailbox,
                                           message["id"])["items"] if children else []
        except (NotVisible, SourceUnavailable, ReadOnlyGuardError, ValueError) as exc:
            status, diagnosis = _failure(exc, self.api.failure)
            self._fail_tree(row, status, diagnosis, **seen)
            return
        body = (message.get("body_text") or "").encode("utf-8")
        ingest, _ = self._ingest(body, f"{row.get('name') or 'message'}.txt")
        truncated = bool(message.get("body_truncated"))
        self._done(self._receipt(
            row, body_status="capped" if truncated else "read", bytes=len(body),
            sha256=sha256(body).hexdigest(), match=None, match_basis=basis,
            detail="exceeds_cap" if truncated else None if message["id"] == row[
                "source"]["mail_message_id"] else f"located_by_{basis}",
            **ingest, **seen))
        for child in children:
            named = [item for item in listed if item.get("name") == child["name"]]
            if len(named) != 1:
                self._fail_tree(child, "gone" if not named else "unavailable",
                                {"detail": "attachment_not_located" if not named
                                 else "attachment_ambiguous"}, **seen)
                continue
            self.api.failure = None
            try:
                read = mail.get_attachment(self.actor, self.mailbox, message["id"],
                                           named[0]["id"])
            except (NotVisible, SourceUnavailable, ReadOnlyGuardError, ValueError) as exc:
                status, diagnosis = _failure(exc, self.api.failure)
                self._fail_tree(child, status, diagnosis, **seen)
                continue
            self._verified(child, read["content"], dict(seen))

    def _verified(self, row: Mapping[str, Any], content: bytes, seen: dict[str, Any]) -> None:
        """Compare the bytes read with the registry (SHA-256 and size; a capture: size and
        the provider's quickXorHash), then store and extract them. Another revision's
        bytes: the row reads ``mismatch`` and the served bytes are their own revision
        entry (``rv-``, ``revision_of`` the row), stored and extracted."""
        from integrations.graph_read import quick_xor_hash
        from operating.use_case_parts import members

        digest = sha256(content).hexdigest()
        expected = row["expected"]
        if expected.get("sha256"):
            basis = "sha256"
            match = digest == expected["sha256"] and len(content) == expected["bytes"]
        else:  # a capture that names no SHA-256: its provider size and quickXorHash
            listed = (row.get("revision") or {}).get("quick_xor_hash")
            basis = "quick_xor_hash" if listed else "bytes"
            match = len(content) == expected.get("bytes") and (
                not listed or quick_xor_hash(content) == listed)
        seen = {**seen, "bytes": len(content), "sha256": digest, "match": match,
                "match_basis": basis}
        if not match:
            revision = self._revision(row, content, seen)
            self._fail_tree(row, "mismatch", {"detail": f"{basis}_mismatch"},
                            current_revision=revision, **seen)
            return
        ingest, accounting = self._ingest(content, row["name"])
        seen.update(ingest)
        found = None
        if row.get("origin") == "capture" or self.children.get(row["id"]):
            found = members(content, row["name"], max_part_bytes=MAX_PART_BYTES)
        if row.get("origin") == "capture":
            seen["member_manifest"] = self._manifest(found)
        self._done(self._receipt(row, body_status="read", **seen))
        if self.children.get(row["id"]):
            self._members(row, found or {"files": [], "gaps": []}, accounting, "")

    def _revision(self, row: Mapping[str, Any], content: bytes, seen: Mapping[str, Any]
                  ) -> str:
        revision_id = _id("rv", f"{row['id']}@{seen['sha256']}")
        ingest, _ = self._ingest(content, row["name"])
        self._done(self._receipt(row, **{
            **seen, **ingest, "entry_id": revision_id, "status": "revision",
            "revision_of": row["id"], "body_status": "read", "match": None,
            "match_basis": None, "proven_by": [],
            "expected": {"bytes": None, "sha256": None}}))
        return revision_id

    # -- members and attachments, from their container's one read
    def _members(self, container: Mapping[str, Any], found: Mapping[str, Any],
                 accounting: Mapping[str, Any] | None, prefix: str) -> None:
        from operating.use_case_intake import detect_type

        for child in self.children.get(container["id"], []):
            path, located = self._locate(child, found, prefix)
            if located is None:
                gap = next((g for g in found["gaps"] if path and g["path"] == path), None)
                over = bool(gap) and gap["state"] == "skipped_by_cap"
                status = "capped" if over else "unavailable"
                detail = ("exceeds_cap" if over else "member_unreadable" if gap
                          else "not_in_container")
                self._fail_tree(child, status, {"detail": detail})
                continue
            data = located["data"]
            digest = located["sha256"]
            match = (digest == child["expected"]["sha256"]
                     and len(data) == child["expected"]["bytes"])
            seen: dict[str, Any] = {"read_via": "container", "bytes": len(data),
                                    "sha256": digest, "match": match}
            if not match:
                self._fail_tree(child, "mismatch", {"detail": "sha256_mismatch"}, **seen)
                continue
            detected, warnings = detect_type(data, child["name"])
            seen.update(detected_type=detected, detected_media_type=_MEDIA_TYPES.get(detected),
                        type_warnings=warnings, stored=self.store.pin(data))
            if accounting is not None:
                full = f"{self._top_name(container)}!/{located['path']}"
                parts = [p for p in accounting["parts"] if p["path"] == full
                         or p["path"].startswith((full + "!/", full + "#"))]
                text = "\n".join(accounting["text"][p["start"]:p["end"]] for p in parts
                                 if "start" in p)
                seen["extraction"] = {**_extraction(parts), "text_ref": (
                    self.store.write_text(digest, text) if text else None)}
            self._done(self._receipt(child, body_status="read", **seen))
            if self.children.get(child["id"]):
                self._members(child, found, accounting, located["path"])

    def _top_name(self, row: Mapping[str, Any]) -> str:
        while row.get("parent"):
            row = self.by_id[row["parent"]]
        return row["name"]

    @staticmethod
    def _locate(child: Mapping[str, Any], found: Mapping[str, Any], prefix: str
                ) -> tuple[str | None, Mapping[str, Any] | None]:
        """The member file a child row names: an archive member by its path, an attachment
        by its name among its message's direct members (the registered bytes break a tie;
        a name the message encodes differently falls back to the registered bytes)."""
        if child["kind"] == "archive_member" and child.get("member_path"):
            path = f"{prefix}!/{child['member_path']}" if prefix else child["member_path"]
            return path, next((f for f in found["files"] if f["path"] == path), None)
        start = prefix + "!/" if prefix else ""
        direct = [f for f in found["files"] if f["path"].startswith(start)
                  and "!/" not in f["path"][len(start):]]
        named = [f for f in direct if f["name"] == child.get("name")]
        exact = [f for f in (named or direct) if f["sha256"] == child["expected"]["sha256"]]
        located = (exact or named or [None])[0]
        return (located["path"] if located else None), located

    # -- watch folders
    def watch(self, folder: Mapping[str, Any], stop: Any) -> dict[str, Any]:
        """List a watch folder (recursively, to its depth bound). A listed file the
        registry pins is counted registered; every other listed item gets its own
        ``discovered`` receipt (``nr-``): a file is read in full like an original, an item
        with no file facet (a package, a notebook) is ``not_file``. One item listed twice
        (two folders, or two watch folders) is read once and counted ``repeated``."""
        from integrations.live_source_guard import (NotVisible, ReadOnlyGuardError,
                                                    SourceUnavailable)
        from integrations.shared_link import resolve_shared_link

        row: dict[str, Any] = {"id": folder["id"], "status": "listed", "folders": 0, "files": 0,
                               "registered": 0, "discovered": 0, "read": 0, "other": 0,
                               "repeated": 0, "complete": True, "http_status": None,
                               "error_code": None, "detail": None}
        self.api.failure = None
        try:
            if folder.get("drive_id"):  # a nondefault library: drive id and folder path
                drive_id = folder["drive_id"]
                pending: list[tuple[str | None, int]] = [(None, 0)]
            else:
                drive_id, folder_id = resolve_shared_link(
                    self.api, self.transport, self.audience, self.actor, folder["folder_url"])
                pending = [(folder_id, 0)]
            reader = self._drive(drive_id)
            while pending:
                current, depth = pending.pop(0)
                self.api.failure = None
                listing = (reader.list_folder(self.actor, folder["path"],
                                              max_items=WATCH_MAX_ITEMS) if current is None
                           else reader.list_folder(self.actor, folder_id=current,
                                                   max_items=WATCH_MAX_ITEMS))
                row["folders"] += 1
                row["complete"] = row["complete"] and bool(listing["completeness"].get(
                    "complete"))
                for item in listing["items"]:
                    if item["kind"] == "folder":
                        if depth + 1 < int(folder.get("max_depth") or WATCH_MAX_DEPTH):
                            pending.append((item["id"], depth + 1))
                        else:
                            row["complete"] = False
                        continue
                    key = (item.get("drive_id") or drive_id, item["id"])
                    is_file = item["kind"] == "file"
                    row["files" if is_file else "other"] += 1
                    if is_file and key in self.known_items:
                        row["registered"] += 1
                        self.listed_in.setdefault(key, folder["id"])
                        continue
                    entry_id = _id("nr", f"{key[0]}/{key[1]}")
                    if entry_id in self.discovered_ids:
                        row["repeated"] += 1
                        continue
                    self.discovered_ids.add(entry_id)
                    row["discovered"] += 1
                    self.deferred.append((row, folder, {**item, "drive_id": key[0]}, entry_id))
        except (NotVisible, SourceUnavailable, ReadOnlyGuardError) as exc:
            status, diagnosis = _failure(exc, self.api.failure)
            row.update(status=status, complete=False,
                       **{k: diagnosis.get(k) for k in ("http_status", "error_code",
                                                        "detail")})
        return row

    def read_discovered(self, stop: Any) -> None:
        """Read the files ``watch`` listed, least recently read first (never read, or last
        read without success, before the oldest full read), so a folder larger than one
        budget is covered across runs; a file read by an earlier run costs no request."""
        def last_read(entry: tuple) -> str:
            prior = self.store.prior(entry[3])
            return str(prior.get("read_at")) if prior and prior.get("body_status") == "read" \
                else ""

        for row, folder, item, entry_id in sorted(self.deferred, key=last_read):
            row["read"] += self._discovered(folder, item, entry_id, stop) == "read"
        self.deferred = []

    def _discovered(self, folder: Mapping[str, Any], item: Mapping[str, Any], entry_id: str,
                    stop: Any) -> str:
        """A listed item the registry does not pin: read in full through
        ``SharePointReader.get_content`` (bytes, SHA-256, eTag, type by bytes, extraction),
        or why not (HTTP status and Graph error code; ``no_file_facet``; ``budget_cut``).
        A person decides whether it joins the registry."""
        from integrations.live_source_guard import (NotVisible, ReadOnlyGuardError,
                                                    SourceUnavailable)
        from operating.use_case_intake import MAX_CONTENT_BYTES

        row = {"id": entry_id, "name": item.get("name"), "kind": None,
               "mime_type": item.get("mime_type"),
               "source": {"url": item.get("original_url"), "drive_id": item["drive_id"],
                          "item_id": item["id"]}}
        base: dict[str, Any] = {"status": "discovered", "watch_folder": folder["id"],
                                "read_via": "drive_item", "listed_bytes": item.get("size"),
                                "provider_revision": {"etag": item.get("etag")},
                                "last_modified": item.get("last_modified")}
        if item["kind"] != "file":
            self._done(self._receipt(row, body_status="not_file", detail="no_file_facet",
                                     **base))
            return "not_file"
        reused = self._unchanged(entry_id, item, base) if self.reuse_unchanged else None
        if reused is not None:
            self.discovered_reused += 1
            self._done(reused)
            return "read"
        if stop():
            return self._unreached(self._receipt(row, body_status="unavailable",
                                                 detail="budget_cut", **base))
        self.api.failure = None
        try:
            read = self._drive(item["drive_id"]).get_content(
                self.actor, item["id"], max_bytes=MAX_CONTENT_BYTES, content_route=True)
        except (NotVisible, SourceUnavailable, ReadOnlyGuardError) as exc:
            status, diagnosis = _failure(exc, self.api.failure)
            if diagnosis["detail"] == "budget_cut":
                self.api.budget_cuts -= 1  # a discovered file left pending, not a run cut
                return self._unreached(self._receipt(row, body_status=status, **diagnosis,
                                                     **base))
            self._done(self._receipt(row, body_status=status, **diagnosis, **base))
            return status
        record = read["record"]
        base["provider_revision"] = {**base["provider_revision"],
                                     "etag_read": record.get("etag")}
        if read.get("content_status") != "read":
            self._done(self._receipt(row, body_status="capped", detail="exceeds_cap",
                                     bytes=record.get("size"), **base))
            return "capped"
        content = read["content"]
        cut_before = self.ocr_cut
        ingest, _ = self._ingest(content, item.get("name"))
        if self.ocr_cut > cut_before:  # read, but not fully extracted: read again next run
            self.discovered_ocr_cut += 1
            self.ocr_cut = cut_before
        self.discovered_new += 1
        self._done(self._receipt(row, body_status="read", bytes=len(content),
                                 sha256=sha256(content).hexdigest(), **ingest, **base))
        return "read"

    def _unreached(self, receipt: dict[str, Any]) -> str:
        """A discovered file the budget left unread: reported, but its receipt is not
        stored, so an earlier full read of it stays reusable and keeps its read time."""
        self.discovered_unreached += 1
        self.extra.append(receipt)
        return "unavailable"

    def _unchanged(self, entry_id: str, item: Mapping[str, Any], base: Mapping[str, Any]
                   ) -> dict[str, Any] | None:
        """An earlier run's receipt of a full read of this discovered file (any registry
        version, same drive and item), reused without any request when the listing names the
        eTag that read answered and the size it read, and its stored bytes re-hash with
        their text stored: read again only when the file changed or its store did. An
        extraction the budget cut is never reused. The receipt keeps its read time and
        reader and says it was reused (``reused``: basis, when and by whom)."""
        prior = self.store.prior(entry_id)
        if prior is None or prior.get("schema") != RECEIPT_SCHEMA \
                or prior.get("status") != "discovered" or prior.get("body_status") != "read":
            return None
        source, revision = prior.get("canonical_source") or {}, prior.get("provider_revision") or {}
        extraction = prior.get("extraction") or {}
        etag = item.get("etag")
        if (not isinstance(etag, str) or not etag or revision.get("etag_read") != etag
                or (source.get("drive_id"), source.get("item_id")) != (item["drive_id"],
                                                                       item["id"])
                or not isinstance(prior.get("bytes"), int) or prior["bytes"] != item.get("size")
                or "cap:budget_seconds" in (extraction.get("unread_reasons") or ())
                or not self.store.intact(prior.get("sha256"), prior.get("stored"),
                                         extraction.get("text_ref"))):
            return None
        return {**prior, **base, "name": item.get("name"), "registry_version": self.version,
                "provider_revision": {**revision, "etag": etag},
                "reused": {"basis": "etag_unchanged",
                           "listed_at": self.clock().astimezone(_UTC).isoformat(),
                           "reader": dict(self.reader)}}


def _instant(value: Any) -> datetime | None:
    """An ISO instant to the second (UTC), or None."""
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(
            _UTC).replace(microsecond=0)
    except ValueError:
        return None


def _strings(node: Any) -> Any:
    """Every string leaf of a JSON document."""
    stack = [node]
    while stack:
        current = stack.pop()
        if isinstance(current, str):
            yield current
        elif isinstance(current, Mapping):
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)


def _identity(environ: Mapping[str, str], run_id: str, role: str,
              environment: str | None) -> dict[str, Any]:
    if not _TOKEN.fullmatch(run_id or ""):
        raise ValueError("run_id_invalid")
    if role not in ROLES:
        raise ValueError("role_invalid")
    if environment is not None and not re.fullmatch(r"[A-Za-z0-9_.: \-]{1,64}", environment):
        raise ValueError("environment_invalid")
    identity: dict[str, Any] = {}
    for key, name in IDENTITY_ENV.items():
        value = (environ.get(name) or "").strip()
        identity[key] = value if _TOKEN.fullmatch(value) else None
    return {**identity, "run_id": run_id, "role": role, "environment": environment}


DISCOVERED_STATUSES = ("read", "partial", "capped", "denied", "gone", "unavailable",
                       "not_file")
READ_ROUTES = ("drive_item", "drive_path", "drive_item_version", "mail", "shared_link",
               "container", "repository")
READ_STATUSES = ("read", "discovered_read", "revision_read")


def _routed(row: Mapping[str, Any]) -> str | None:
    """The route an ``unfetched`` row is read through (``NO_ROUTE_REASONS``), or None."""
    source, reason = row.get("source") or {}, row.get("unfetched_reason")
    if reason == "historical_revision":
        version = (row.get("revision") or {}).get("version_id")
        if source.get("drive_id") and source.get("item_id") and version:
            return "drive_item_version"
    if reason == "historical_mail_pin":
        if row.get("kind") == "mail_message" and source.get("mail_message_id") \
                and source.get("received_at"):
            return "mail"
        if row.get("kind") == "attachment" and row.get("parent") and row.get("name"):
            return "mail"
    return None


def _cause(entry: Mapping[str, Any]) -> str:
    """One entry's diagnosis as one enum token (status, HTTP status, Graph error code,
    detail, extraction status), at most 64 characters of ``[A-Za-z0-9_.:/-]``."""
    token = ":".join(str(entry.get(k) if entry.get(k) is not None else "-")
                     for k in ("status", "http_status", "error_code", "detail", "extraction"))
    return re.sub(r"[^A-Za-z0-9_.:/\-]", "_", token)[:64]


def read(environ: Mapping[str, str] | None = None, *, registry_path: str | Path = REGISTRY_PATH,
         state_dir: str | Path | None = None, run_id: str, role: str,
         environment: str | None = None, sender: Any = None, clock: Any = None,
         ocr: Any = "local", watch: bool = True, budget_seconds: float | None = None,
         monotonic: Any = None, reuse_unchanged: bool = False,
         sleep: Any = None) -> dict[str, Any]:
    """Read every registered original and every watch-folder file (module docstring);
    the report. ``budget_seconds`` bounds the run: once spent, no further original, watch
    folder or discovered file is started and each one not reached is reported unavailable
    with detail ``budget_cut``. ``budget_cut`` (``ok`` false) means a registry entry, a
    watch-folder listing or a registry file's OCR was cut; discovered files are listed
    first and read least recently read first, and those the budget left are
    ``discovered_pending``, a continuation the next run reads on (``ok`` stays true while
    some file was read this run; false when files are pending and none was). Past the deadline no
    download and no OCR starts either: an original whose metadata was read but whose
    bytes were not yet requested is cut too (``originals_cut_in_read``), and an image
    met later is ``skipped_by_cap`` (``budget_seconds``); each OCR's timeout is at most
    the budget left, and ``ocr`` is then called with ``timeout=``. Only a download
    already started finishes, so a run ends within ``STEP_BOUND_SECONDS`` of the
    budget. ``reuse_unchanged`` (the CLI's default: its store is kept between runs): a
    discovered file listed with the eTag an earlier run read in full is not read again
    (``_Run._unchanged``; ``discovered_reused``), so a folder larger than one budget is
    read across runs instead of from its start every run."""
    from integrations.live_source_guard import BoundedBackoff
    from operating.use_case_parts import local_image_ocr

    env = os.environ if environ is None else environ
    state = state_dir or (env.get(STATE_ENV) or "").strip()
    if not state:
        raise ValueError("state_dir_not_configured")
    reader = _identity(env, run_id, role, environment)
    registry, version = load_registry(registry_path)
    retry = BoundedBackoff(sleep=sleep or time.sleep, budget_seconds=(
        RETRY_BUDGET_SECONDS if budget_seconds is None
        else max(0, min(RETRY_BUDGET_SECONDS, budget_seconds))))
    run = _Run(registry, version, state, env, reader, sender, clock,
               local_image_ocr() if ocr == "local" else ocr, retry)
    run.reuse_unchanged = reuse_unchanged
    monotonic = monotonic or time.monotonic
    started = monotonic()

    def spent() -> bool:
        return budget_seconds is not None and monotonic() - started >= budget_seconds

    if budget_seconds is not None:
        run.remaining = run.api.remaining = (
            lambda: budget_seconds - (monotonic() - started))

    cut = False
    reached = unreached = 0
    for row in run.rows:
        if row.get("parent"):
            continue
        fetchable = row["status"] != "unfetched" or (
            _routed(row) == "mail" and run.mailbox is not None
            or _routed(row) == "drive_item_version")
        if fetchable and (cut := cut or spent()):
            run._fail_tree(row, "unavailable", {"detail": "budget_cut"})
            unreached += 1
            continue
        run.original(row)
        reached += fetchable
    folders = []
    folders_unreached = 0
    for folder in (registry.get("watch_folders") or []) if watch else []:
        if cut := cut or spent():
            folders.append({"id": folder["id"], "status": "unavailable", "folders": 0,
                            "files": 0, "registered": 0, "discovered": 0, "read": 0,
                            "other": 0, "repeated": 0, "complete": False,
                            "http_status": None, "error_code": None, "detail": "budget_cut"})
            folders_unreached += 1
        else:
            folders.append(run.watch(folder, spent))
    run.read_discovered(spent)
    cut = cut or run.api.budget_cuts > 0 or run.ocr_cut > 0
    item_folder = {}
    for row_id, key in run.item_of.items():
        if key in run.listed_in:
            item_folder[row_id] = run.listed_in[key]

    def entry(receipt: Mapping[str, Any], row: Mapping[str, Any] | None) -> dict[str, Any]:
        prefix = {"registered": "", "discovered": "discovered_",
                  "revision": "revision_"}[receipt["status"]]
        manifest = receipt.get("member_manifest") or {}
        return {"status": prefix + receipt["body_status"],
                "registered": receipt["status"] == "registered",
                "name": receipt.get("name"),
                "watch_folder": receipt.get("watch_folder") or item_folder.get(
                    receipt["entry_id"]),
                "case": (row or {}).get("case"), "kind": (row or {}).get("kind"),
                "source": receipt["read_via"], "sha256_match": receipt["match"],
                "match_basis": receipt.get("match_basis"),
                "http_status": receipt["http_status"], "error_code": receipt["error_code"],
                "detail": receipt["detail"], "extraction": receipt["extraction"]["status"],
                "revision_of": receipt.get("revision_of"),
                "current_revision": receipt.get("current_revision"),
                "members": manifest.get("counts")}

    registered = [(row["id"], entry(run.results[row["id"]], row)) for row in run.rows
                  if row["id"] in run.results]
    extra = [(r["entry_id"], entry(r, None)) for r in run.extra]
    entries = registered + extra
    failed = [e for _, e in entries if e["status"] not in READ_STATUSES]
    by_status = {status: sum(e["status"] == status for _, e in entries)
                 for status in (*BODY_STATUSES,
                                *(f"discovered_{s}" for s in DISCOVERED_STATUSES),
                                "revision_read")}
    problems = [(entry_id, _cause(e)) for entry_id, e in entries
                if e["status"] not in READ_STATUSES or e["sha256_match"] is False]
    causes: dict[str, int] = {}
    for _, cause in problems:
        causes[cause] = causes.get(cause, 0) + 1
    captures = {}
    for row in run.rows:
        if row.get("origin") != "capture" or row.get("parent"):
            continue
        receipt = run.results.get(row["id"]) or {}
        counts = (receipt.get("member_manifest") or {}).get("counts") or {}
        captures[row["id"]] = {
            "status": receipt.get("body_status"), "registry_status": row["status"],
            "bytes": receipt.get("bytes"), "match": receipt.get("match"),
            "members": counts.get("read"), "member_oversize": counts.get("oversize"),
            "member_unread": counts.get("unread"), "detail": receipt.get("detail")}
    report = {
        "check": REPORT_CHECK, "evidence_label": "live-read", "read_only_guard": READ_ONLY_GUARD,
        "registry_version": version, "registry_rows": len(registry["rows"]),
        "entries": len(run.rows), "reported": len(registered),
        "unreported": len(run.rows) - len(registered),
        "gaps": sum(r["status"] == "gap" for r in registry["rows"]),
        "by_status": by_status,
        "sha256_match": sum(1 for _, e in registered if e["sha256_match"] is True),
        # files read, by route: the live floor asks for a Graph read, not only the Git one
        # (a rendering kept as a text field of a committed record is the repository route)
        "read_via": {via: sum(e["status"] == "read"
                              and {"repository_field": "repository"}.get(e["source"],
                                                                         e["source"]) == via
                              for _, e in registered) for via in READ_ROUTES},
        "undiagnosed": sum(1 for e in failed if not (e["http_status"] or e["error_code"]
                                                    or e["detail"])),
        "extraction": {status: sum(e["extraction"] == status for _, e in entries)
                       for status in EXTRACTION_STATUSES},
        "failure_causes": causes,
        "problem_pages": [dict(problems[i:i + PAGE]) for i in range(0, len(problems), PAGE)],
        "captures": captures, "captures_reported": sum(
            c["status"] is not None for c in captures.values()),
        "entry_pages": [dict(entries[i:i + PAGE]) for i in range(0, len(entries), PAGE)],
        "budget_cut": cut, "budget_seconds": budget_seconds,
        "originals_reached": reached, "originals_unreached": unreached,
        "originals_cut_in_read": sum(
            1 for row in run.rows if not row.get("parent")
            and (run.results.get(row["id"]) or {}).get("error_class") == "BudgetCut"),
        "ocr_cut": run.ocr_cut,
        "watch_folders_unreached": folders_unreached,
        "watch_folders": folders,
        "discovered": sum(r["status"] == "discovered" for r in run.extra),
        "discovered_unreached": run.discovered_unreached,
        "discovered_reused": run.discovered_reused,
        "discovered_pending": run.discovered_unreached + run.discovered_ocr_cut,
        "revisions": sum(r["status"] == "revision" for r in run.extra),
        **audit_findings(*render_findings(
            [*run.results.values(), *run.extra], run.store)),
        "item_isolation": item_isolation([e for _, e in entries]),
        "receipts": len(entries), "reader": reader,
        "read_at": run.clock().astimezone(_UTC).isoformat(),
        "requests_sent": run.transport.requests_sent,
        "operations_read": sorted(run.transport.operations_read)}
    # unread discovered files are a continuation, not a cut; but a run that makes no
    # progress on them fails, so a stalled continuation cannot pass
    stalled = report["discovered_pending"] > 0 and run.discovered_new == 0
    report["ok"] = (report["unreported"] == 0 and report["undiagnosed"] == 0 and not cut
                    and not stalled)
    return report


# ------------------------------------------------------------------ CLI

def main(argv: Sequence[str] | None = None, *, environ: Mapping[str, str] | None = None,
         sender: Any = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python3 -m scripts.case_originals", description=__doc__.splitlines()[0],
        epilog=f"Capture store: <state>/{STORE_DIR}/<sha256> (original bytes), "
               f"<state>/{STORE_DIR}/extractions/<sha256>.txt, "
               f"<state>/{STORE_DIR}/receipts/<registry sha256>/<entry id>.json; "
               f"<state> is --state-dir or {STATE_ENV}, never inside Git.")
    commands = parser.add_subparsers(dest="command", required=True)
    gen = commands.add_parser("registry", help="generate the registry from source-pin records")
    gen.add_argument("--from", dest="source", required=True, type=Path)
    gen.add_argument("--advisory-commit", required=True)
    gen.add_argument("--advisory-path", required=True)
    gen.add_argument("--delta", type=Path, help="an incremental source-pin record")
    gen.add_argument("--delta-commit")
    gen.add_argument("--delta-path")
    gen.add_argument("--proven", action="append", default=[], metavar="CHECK_ID=FILE")
    gen.add_argument("--watch-link", action="append", default=[], metavar="URL")
    gen.add_argument("--watch-drive", action="append", default=[], nargs=3,
                     metavar=("DRIVE_ID", "PATH", "WEB_URL"))
    gen.add_argument("--capture", action="append", default=[], nargs=5,
                     metavar=("CASE_ID", "GAP_ID", "COMMIT", "PATH", "FILE"),
                     help="one capture record (plx-advisory/v1 with an original_file) at "
                          "COMMIT:PATH, its bytes in FILE; its row is bound to CASE_ID")
    gen.add_argument("--pin-receipts", type=Path, metavar="DIR",
                     help="full-read receipts (<state>/case-originals/receipts/<version>) "
                          "that pin the unpinned capture rows they verified")
    gen.add_argument("--manifest", action="append", default=[], nargs=4,
                     metavar=("CASE_ID", "COMMIT", "PATH", "FILE"),
                     help="one manifest record (plx-advisory/v1 with source_manifest, "
                          "nested_attachment_manifest, zip_member_manifest) at COMMIT:PATH, "
                          "its bytes in FILE; its rows are bound to CASE_ID")
    gen.add_argument("--caveats", action="append", default=[], type=Path, metavar="FILE",
                     help=f"owner caveats ({CAVEATS_SCHEMA}) on the advisory record they name")
    gen.add_argument("--crosswalk", action="append", default=[], metavar="REPO_PATH",
                     help=f"an eval card file ({CROSSWALK_FORMAT}) by its repository path "
                          "(relative); its sources are mapped to registry rows")
    gen.add_argument("--card-sources", metavar="REPO_PATH",
                     help=f"the originals eval cards name ({CARD_SOURCES_SCHEMA}); the "
                          "advisory records it cites are read from the local Git objects")
    gen.add_argument("--out", type=Path, default=REGISTRY_PATH)
    run = commands.add_parser(
        "read", help="read every registered original (GET only)",
        epilog=f"Writes <state>/{STORE_DIR}/<sha256>, <state>/{STORE_DIR}/extractions/ and "
               f"<state>/{STORE_DIR}/receipts/<registry sha256>/<entry id>.json; <state> is "
               f"--state-dir or {STATE_ENV} (the live verifier's capture store).")
    run.add_argument("--registry", type=Path, default=REGISTRY_PATH)
    run.add_argument("--state-dir", help=f"capture store (default: {STATE_ENV})")
    run.add_argument("--run-id", required=True)
    run.add_argument("--role", required=True, choices=ROLES)
    run.add_argument("--environment")
    run.add_argument("--no-watch", action="store_true")
    run.add_argument("--budget-seconds", type=float,
                     help="start no further original or watch folder after this many seconds; "
                          "those not reached are reported budget_cut (exit 1)")
    run.add_argument("--reread-unchanged", action="store_true",
                     help="read every discovered file again, even one listed with the eTag "
                          "an earlier run's stored full read answered")
    args = parser.parse_args(argv)
    if args.command == "registry":
        command = ("python3 -m scripts.case_originals registry --from <baseline.json> "
                   f"--advisory-commit {args.advisory_commit} "
                   f"--advisory-path {args.advisory_path}"
                   + (f" --delta <delta.json> --delta-commit {args.delta_commit} "
                      f"--delta-path {args.delta_path}" if args.delta else "")
                   + "".join(f" --proven {p}" for p in args.proven)
                   + "".join(f" --watch-link {u}" for u in args.watch_link)
                   + "".join(" --watch-drive " + " ".join(f"'{x}'" for x in d)
                             for d in args.watch_drive)
                   + "".join(f" --capture {c} {g} {m} {p} <capture.json>"
                             for c, g, m, p, _ in args.capture)
                   + (" --pin-receipts <receipts>" if args.pin_receipts else "")
                   + "".join(f" --manifest {c} {m} {p} <manifest.json>"
                             for c, m, p, _ in args.manifest)
                   + " --caveats <caveats.json>" * len(args.caveats)
                   + "".join(f" --crosswalk {x}" for x in args.crosswalk)
                   + (f" --card-sources {args.card_sources}" if args.card_sources else ""))
        try:
            receipts = ([json.loads(f.read_bytes())
                         for f in sorted(args.pin_receipts.glob("*.json"))]
                        if args.pin_receipts else [])
            captures = [(c, g, Path(f).read_bytes(), m, p) for c, g, m, p, f in args.capture]
            manifests = [(c, Path(f).read_bytes(), m, p) for c, m, p, f in args.manifest]
            notes = [f.read_bytes() for f in args.caveats]
            for x in args.crosswalk:
                if not _REPO_PATH.fullmatch(x) or ".." in x.split("/"):
                    raise ValueError("crosswalk_input_invalid")
            crosswalks = [(x, (REPOSITORY_ROOT / x).read_bytes()) for x in args.crosswalk]
            card_sources, card_records = None, {}
            if args.card_sources:
                x = args.card_sources
                if not _REPO_PATH.fullmatch(x) or ".." in x.split("/"):
                    raise ValueError("card_sources_input_invalid")
                card_sources = (REPOSITORY_ROOT / x).read_bytes()
                card_records = _card_records(card_sources)
            registry = build_registry(
                args.source.read_bytes(), advisory_commit=args.advisory_commit,
                advisory_path=args.advisory_path,
                delta_bytes=args.delta.read_bytes() if args.delta else None,
                delta_commit=args.delta_commit, delta_path=args.delta_path,
                proven=_proven(args.proven), watch_links=args.watch_link,
                watch_drives=args.watch_drive, captures=captures, receipts=receipts,
                manifests=manifests, caveats=notes, crosswalks=crosswalks,
                card_sources=card_sources, card_records=card_records, command=command)
        except (OSError, ValueError) as exc:
            print(json.dumps({"error": "registry_invalid", "reason": str(exc)[:80]}),
                  file=sys.stderr)
            return 1
        args.out.write_text(json.dumps(registry, indent=1, ensure_ascii=False) + "\n",
                            encoding="utf-8")
        print(json.dumps({"counts": registry["counts"]}))
        return 0
    from integrations.live_source_guard import ReadOnlyGuardError, SourceUnavailable
    try:
        report = read(environ, registry_path=args.registry, state_dir=args.state_dir,
                      run_id=args.run_id, role=args.role, environment=args.environment,
                      sender=sender, watch=not args.no_watch,
                      budget_seconds=args.budget_seconds,
                      reuse_unchanged=not args.reread_unchanged)
    except ReadOnlyGuardError:
        print(json.dumps({"error": "read_only_guard", "rule": "get_only"}), file=sys.stderr)
        return 2
    except (OSError, ValueError, SourceUnavailable) as exc:
        code = str(exc) if _TOKEN.fullmatch(str(exc)) else type(exc).__name__
        print(json.dumps({"error": code}), file=sys.stderr)
        return 1
    print(json.dumps(report, indent=1, sort_keys=True))
    return 0 if report["ok"] else 1


__all__ = ["BODY_STATUSES", "FILE_STATUSES", "REGISTRY_PATH", "SCHEMA", "build_registry",
           "load_registry", "main", "read"]


if __name__ == "__main__":
    raise SystemExit(main())
