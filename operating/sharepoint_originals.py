"""Originals in one SharePoint folder, found by file name, as cited case originals.

A folder of scanned packets (for example a Quality site's assembly-order packets) is
described by an expected-identity file (``plx-sharepoint-originals-expected/v1``): the
drive and folder, the file-name pattern whose one group is the record code a packet
belongs to, and the identity (item id, bytes, SHA-256) an earlier audit saw per file.
That file is evaluation data the caller names (``--expected``); this module holds no
folder, drive, code or digest. It adds no HTTP: it reads only through the existing
GET-only Graph adapter (``integrations.graph_read.SharePointReader``), either

* inside a case: the session actor's audience-checked folder listing (``live.list``,
  ``operating.source_reads``; a ``sharepoint://`` citation per item), then the case
  tool ``plx.case_attach_original`` with drive_id + item_id (bytes under a cap,
  SHA-256 identity, immutable provenance, citable as ``original://<id>`` and
  ``original://<id>#page=<n>``) -- ``attach_original`` / ``attach_originals``;
* or, for the live verifier, directly through the reader with a probe audience that
  grants only that folder's top-level tree -- ``check``
  (``python3 -m operating.sharepoint_originals check --expected <file>``), which prints
  counts, byte sizes and SHA-256 digests only (no text, no names of people, no URLs) and
  exits 0 only when every expected file was read and matches its expected SHA-256.

A packet is matched by its file name, never by a hardcoded item id; a listed item whose
id or digest differs from the expectation is reported, not hidden.

Evidence label: everything here is live-unverified until the live verifier runs the
``devloop/live-checks/ao-originals.json`` spec; tests use an injected transport.
"""

from __future__ import annotations

import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import sys
from typing import Any, Callable, Mapping, Sequence

EXPECTED_SCHEMA = "plx-sharepoint-originals-expected/v1"
LIST_LIMIT = 200
PROBE_PRINCIPAL = "probe:originals"
# ``drive_env`` may name only a drive-id variable, never a credential; a drive id goes into a
# Graph URL path, so it is one path segment of drive-id characters.
DRIVE_ENV_NAME = re.compile(r"^PLX_[A-Z0-9_]*DRIVE_ID$")
DRIVE_ID = re.compile(r"^[A-Za-z0-9!_-]{1,200}$")

Call = Callable[[str, dict[str, Any]], dict[str, Any]]


def load_expected(path: str | Path) -> dict[str, Any]:
    """The expected-identity file, strictly checked (``ValueError`` names the fault)."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if (data.get("schema") != EXPECTED_SCHEMA or not isinstance(data.get("files"), list)
            or not all(isinstance(data.get(key), str) and data[key]
                       for key in ("drive_id", "folder", "name_pattern"))):
        raise ValueError("sharepoint_originals_expected_schema")
    drive_env = data.get("drive_env")
    if drive_env is not None and (not isinstance(drive_env, str)
                                  or DRIVE_ENV_NAME.fullmatch(drive_env) is None):
        raise ValueError("sharepoint_originals_drive_env_refused")
    _drive_id(data["drive_id"])
    pattern = name_pattern(data)
    for item in data["files"]:
        if (not isinstance(item, Mapping) or pattern.fullmatch(str(item.get("name"))) is None
                or not re.fullmatch(r"[0-9a-f]{64}", str(item.get("sha256")))):
            raise ValueError("sharepoint_originals_expected_file_invalid")
    return data


def name_pattern(expected: Mapping[str, Any]) -> re.Pattern[str]:
    """The file-name pattern; its one group is the record code a file belongs to."""
    try:
        pattern = re.compile(expected["name_pattern"])
    except (re.error, TypeError) as error:
        raise ValueError("sharepoint_originals_name_pattern_invalid") from error
    if pattern.groups != 1:
        raise ValueError("sharepoint_originals_name_pattern_needs_one_group")
    return pattern


def _drive_id(value: str) -> str:
    """``value`` when it is shaped like a drive id; the error never echoes it."""
    if DRIVE_ID.fullmatch(value) is None:
        raise ValueError("sharepoint_originals_drive_id_invalid")
    return value


def folder_source(expected_path: str | Path, environ: Mapping[str, str] | None = None,
                  drive_id: str | None = None) -> dict[str, Any]:
    """What a case session needs to attach the folder's originals: the drive (an explicit
    ``drive_id``, else the environment variable the file names in ``drive_env``, else the
    file's own), the folder, the name pattern and the expected identity."""
    env = os.environ if environ is None else environ
    expected = load_expected(expected_path)
    drive_env = expected.get("drive_env")
    drive = (drive_id or (env.get(drive_env) if drive_env else None) or "").strip()
    return {"drive_id": _drive_id(drive or expected["drive_id"]), "folder": expected["folder"],
            "name_pattern": expected["name_pattern"], "expected": expected}


def _by_name(expected: Mapping[str, Any] | None) -> dict[str, Mapping[str, Any]]:
    return {item["name"]: item for item in (expected or {}).get("files", [])}


def _refusal(error: Exception) -> str:
    text = str(error)
    return "source_not_configured" if "source_not_configured" in text else "unavailable"


def diagnose_refusal(audience: Any, actor: Any, drive_id: str, folder: str,
                     name: str | None = None) -> str:
    """Which policy refused an ``unavailable`` original, for the operator running a CLI.

    The tools answer "not visible", "does not exist" and "source down" the same on
    purpose; the host that holds the audience file and the actor can still tell its own
    policy apart without any source I/O: ``no_grant`` (no pattern grants the folder, or
    the file in it, to any of the actor's principals in its tenant), ``clearance`` (a
    grant exists but the classification is above the actor's clearance) or
    ``source_error`` (the policy allows the read, so SharePoint refused or failed it)."""
    from dataclasses import replace

    base = folder.rstrip("/") or "/"
    key_sets = [[f"sharepoint:{drive_id}:{base}", f"sharepoint:{drive_id}:{base}/"]]
    if name:
        key_sets.append([f"sharepoint:{drive_id}:{base}/{name}"])
    cleared = replace(actor, clearance="restricted")  # the highest clearance there is
    if not all(audience.allows_all(cleared, keys) for keys in key_sets):
        return "no_grant"
    if not all(audience.allows_all(actor, keys) for keys in key_sets):
        return "clearance"
    return "source_error"


def list_originals(call: Call, drive_id: str, folder: str,
                   pattern: str | re.Pattern[str]) -> dict[str, Any]:
    """The folder's files whose name matches ``pattern``, as the session actor may see them.

    ``status`` is ``listed``, ``source_not_configured`` or ``unavailable`` (not
    visible, nonexistent and source down read the same); ``completeness`` is the
    reader's (truncated listings and withheld items are counted there)."""
    pattern = re.compile(pattern)
    try:
        listed = call("live.list", {"source": "sharepoint", "drive_id": drive_id,
                                    "path": folder, "limit": LIST_LIMIT})
    except RuntimeError as error:
        return {"status": _refusal(error), "items": [], "other_items": 0, "completeness": None}
    items, other = [], 0
    for item in listed.get("items", []):
        match = pattern.fullmatch(str(item.get("name") or ""))
        if item.get("kind") != "file" or match is None:
            other += 1
            continue
        items.append({"code": match.group(1), "name": item["name"], "item_id": item["id"],
                      "size": item.get("size"), "path": item.get("path"),
                      "citation": item.get("citation")})
    return {"status": "listed", "items": items, "other_items": other,
            "completeness": listed.get("completeness")}


def attach_original(call: Call, case_id: str, code: str, *, drive_id: str, folder: str,
                    name_pattern: str | re.Pattern[str],
                    expected: Mapping[str, Any] | None = None,
                    listing: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Attach the file of record ``code`` to the case as a cited original.

    ``status``: ``attached``; ``not_listed`` (the folder listing for this actor has
    no such file -- not proof that none exists: see ``completeness``); ``ambiguous``
    (several files carry that code); ``source_not_configured``; ``unavailable``. The
    attach tool itself records the fact finding citing ``original://<id>`` and the
    ``sharepoint://`` item version."""
    listing = (listing if listing is not None
               else list_originals(call, drive_id, folder, name_pattern))
    out: dict[str, Any] = {"code": code, "status": listing["status"],
                           "completeness": listing.get("completeness")}
    if listing["status"] != "listed":
        return out
    found = [item for item in listing["items"] if item["code"] == code]
    if len(found) != 1:
        out["status"] = "not_listed" if not found else "ambiguous"
        return out
    item = found[0]
    try:
        attached = call("plx.case_attach_original",
                        {"case_id": case_id, "drive_id": drive_id, "item_id": item["item_id"]})
    except RuntimeError as error:
        out["status"] = _refusal(error)
        return out
    original = attached["original"]
    want = _by_name(expected).get(item["name"])
    out.update(status="attached", name=item["name"], item_id=item["item_id"],
               citation=original["citation"], page_citations=list(original["page_citations"]),
               sha256=original["sha256"], bytes=original["bytes"],
               source_citation=(original.get("provenance") or {}).get("source_citation"),
               extraction=original.get("extraction"),
               expected_match=None if want is None else {
                   "sha256": original["sha256"] == want["sha256"],
                   "item_id": item["item_id"] == want.get("item_id"),
                   "bytes": original["bytes"] == want.get("bytes")})
    return out


def attach_originals(call: Call, case_id: str, *, drive_id: str, folder: str,
                     name_pattern: str | re.Pattern[str], codes: Sequence[str] | None = None,
                     expected: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Every matching file in the folder (or those of ``codes``) attached to one case."""
    listing = list_originals(call, drive_id, folder, name_pattern)
    wanted = sorted({item["code"] for item in listing["items"]} if codes is None
                    else set(codes))
    results = [attach_original(call, case_id, code, drive_id=drive_id, folder=folder,
                               name_pattern=name_pattern, expected=expected, listing=listing)
               for code in wanted]
    return {"status": listing["status"], "completeness": listing.get("completeness"),
            "other_items": listing.get("other_items", 0), "originals": results}


def page_text(call: Call, page_citation: str, *, max_pages: int = 50) -> str | None:
    """The whole (machine-read) text of one original page, paged to the end; None
    when the page does not resolve for the session actor."""
    text, offset = "", 0
    for _ in range(max_pages):
        view = call("plx.source_citation", {"citation": page_citation, "text_offset": offset})
        if not view.get("ok"):
            return None
        text += view.get("text") or ""
        offset = view.get("next_text_offset")
        if offset is None:
            return text
    return None  # longer than this reader pages; never a partial text


def page_citation(original: Mapping[str, Any] | None, page: Any) -> str | None:
    """The attached original's citation for ``page`` when its extraction has that page."""
    if not original or original.get("status") != "attached" or not isinstance(page, int):
        return None
    suffix = f"#page={page}"
    return next((handle for handle in original.get("page_citations", [])
                 if handle.endswith(suffix)), None)


# ------------------------------------------------------------------ live check

def check(expected_path: str | Path, environ: Mapping[str, str] | None = None, *,
          sender: Any = None) -> dict[str, Any]:
    """Read every matching file of the folder through the GET-only reader; counts and
    digests only. A probe audience grants one probe principal ``sharepoint:<drive>:/<top>/*``
    (the folder's whole top-level tree); only the folder itself is listed. A file whose
    read fails is still one ``files`` row, with ``failure_diagnostics`` (class, HTTP
    status, error-code class, detail token) in place of its digest, so a failure names the
    file and why, never a message."""
    from integrations.live_source_guard import (LiveSourceAudience, NotVisible,
                                                SourceUnavailable, failure_diagnostics)
    from ontology.core import Actor
    from .case_runtime import build_live_readers

    env = os.environ if environ is None else environ
    source = folder_source(expected_path, env)
    expected, drive, folder = source["expected"], source["drive_id"], source["folder"]
    pattern = name_pattern(expected)
    top = "/" + folder.strip("/").split("/", 1)[0]
    audience = LiveSourceAudience({f"sharepoint:{drive}:{top}/*": [PROBE_PRINCIPAL]},
                                  tenant="probe")
    readers = build_live_readers(audience, env, sharepoint_drives=[drive], graph_sender=sender)
    reader = readers.sharepoint.get(drive)
    report: dict[str, Any] = {
        "check": "sharepoint-originals", "source": "sharepoint-drive",
        "evidence_label": "live-read", "read_only_guard": "GET-only; token POST only",
        "folder": folder, "expected_files": len(expected["files"]),
        "configured": reader is not None}
    if reader is None:
        report["error"] = "source_not_configured"
        return report
    actor = Actor("probe", frozenset({PROBE_PRINCIPAL}), "restricted")
    listing = reader.list_folder(actor, folder, page_size=200, max_pages=2, max_items=400)
    want = _by_name(expected)
    files = [item for item in listing["items"]
             if item.get("kind") == "file" and pattern.fullmatch(str(item.get("name")))]
    report.update(listed_items=len(listing["items"]), matching_files=len(files),
                  completeness=listing["completeness"])
    results = []
    for item in sorted(files, key=lambda entry: entry["name"]):
        expect = want.get(item["name"]) or {}
        try:
            read = reader.get_content(actor, item["id"])
        except (SourceUnavailable, NotVisible) as error:
            results.append({"name": item["name"], "bytes": None, "content_status": "error",
                            "sha256": None, "pdf": False, "sha256_matches_expected": False,
                            "item_id_matches_expected": item["id"] == expect.get("item_id"),
                            **failure_diagnostics(error)})
            continue
        content = read.get("content")
        digest = sha256(content).hexdigest() if content is not None else None
        results.append({"name": item["name"], "bytes": len(content) if content else None,
                        "content_status": read.get("content_status"), "sha256": digest,
                        "pdf": bool(content and content.startswith(b"%PDF-")),
                        "sha256_matches_expected": digest is not None and
                        digest == expect.get("sha256"),
                        "item_id_matches_expected": item["id"] == expect.get("item_id")})
    report["files"] = results
    report["read_errors"] = sum(1 for item in results if item["content_status"] == "error")
    report["read"] = sum(1 for item in results if item["sha256"])
    report["sha256_match_expected"] = sum(1 for item in results
                                          if item["sha256_matches_expected"])
    report["item_id_match_expected"] = sum(1 for item in results
                                           if item["item_id_matches_expected"])
    report["missing_expected"] = sorted(set(want) - {item["name"] for item in results})
    report["bytes_total"] = sum(item["bytes"] or 0 for item in results)
    report["requests_sent"] = getattr(getattr(reader, "_transport", None), "requests_sent", None)
    report["ok"] = (not report["missing_expected"]
                    and report["sha256_match_expected"] == len(expected["files"]))
    return report


def _source_error(error: Exception) -> int:
    """The live verifier publishes error, http_status and error_code from this line."""
    from integrations.live_source_guard import failure_diagnostics
    diagnosis = {key: value for key, value in failure_diagnostics(error).items()
                 if value is not None and key != "error_class"}
    print(json.dumps({"error": type(error).__name__, **diagnosis}), file=sys.stderr)
    return 1


def main(argv: Sequence[str] | None = None, *, environ: Mapping[str, str] | None = None,
         sender: Any = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m operating.sharepoint_originals",
                                     description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("check", help="read the folder's originals (GET only); print "
                                            "counts and SHA-256 digests only")
    run.add_argument("--expected", type=Path, required=True,
                     help="expected-identity file (plx-sharepoint-originals-expected/v1)")
    args = parser.parse_args(argv)
    from integrations.http import SourceUnavailable
    from integrations.live_source_guard import NotVisible, ReadOnlyGuardError
    try:
        report = check(args.expected, environ, sender=sender)
    except ValueError as error:
        code = str(error)
        if not (code.startswith("sharepoint_originals_") and code.isidentifier()):
            return _source_error(error)
        # The expected file was refused before any request: a fixed code, never a value.
        print(json.dumps({"error": "expected_file_refused", "code": code}), file=sys.stderr)
        return 1
    except ReadOnlyGuardError:
        print(json.dumps({"error": "read_only_guard", "rule": "get_only"}), file=sys.stderr)
        return 2
    except (SourceUnavailable, NotVisible) as error:
        return _source_error(error)
    print(json.dumps(report, indent=1, sort_keys=True))
    return 0 if report.get("ok") else 1


__all__ = ["EXPECTED_SCHEMA", "attach_original", "attach_originals", "check",
           "diagnose_refusal", "folder_source", "list_originals", "load_expected", "main",
           "name_pattern", "page_citation", "page_text"]


if __name__ == "__main__":
    raise SystemExit(main())
