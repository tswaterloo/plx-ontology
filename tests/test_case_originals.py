"""Case originals registry and reader (scripts/case_originals.py): registry rows from a
trimmed copy of the source-pin record and capture records, and one read of every
registered original and every watch-folder file through the GET-only Graph clients over a
fixture tenant (injected transport)."""

from __future__ import annotations

import base64
from email.message import EmailMessage
from hashlib import sha256
import io
import json
from pathlib import Path
import re
import subprocess
import tempfile
import unittest
import urllib.error
from unittest import mock
from urllib.parse import parse_qs, quote, unquote, urlsplit
import uuid
import zipfile

from integrations.graph_read import quick_xor_hash
from integrations.live_source_grammar import encode_share_id
from integrations.tests import test_live_source_adapters as adapters
from devloop import live_verify as lv
from scripts import case_originals as co

SECRET = "s3cr3t~CLIENT~value~do-not-leak"
TOKEN = "eyJ0eXAi.ACCESS-TOKEN-do-not-leak.sig"
TEMPAUTH = "DOWNLOAD-TOKEN-do-not-leak"
SP, OD = "plxtenant.sharepoint.com", "plxtenant-my.sharepoint.com"
DRIVE, PERSONAL = "b!drive-AbC123", "b!personal-OnE_drive"
ENV = {"PLX_EXCLUDED_PEOPLE_OIDS": "0e0e0e0e-0000-4000-8000-00000000e0e1",
       "MICROSOFT_GRAPH_TENANT_ID": "11111111-2222-3333-4444-555555555555",
       "MICROSOFT_GRAPH_CLIENT_ID": "11111111-2222-3333-4444-555555555555",
       "MICROSOFT_GRAPH_CLIENT_SECRET": SECRET,
       "SHAREPOINT_SITE_ID": f"{SP},11111111-0000-0000-0000-000000000001,"
                             "22222222-0000-0000-0000-000000000002",
       "CLAUDE_CODE_ACCOUNT_UUID": "aaaaaaaa-0000-4000-8000-00000000000b",
       "CLAUDE_CODE_REMOTE_SESSION_ID": "session_01VerifierB",
       "CLAUDE_CODE_CONTAINER_ID": "container_77"}
COMMIT = "c" * 40

MAP = "# Lifecycle map\nStep 1: the label proof is approved.\nUnique body sentence 9F3.\n".encode()
TEXT_EXPORT = b"From: printer\nThe proof thread text export.\n"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64  # bytes of a PNG, registered with a .pdf name
WORKBOOK = b"PK-not-read"
HTML_SERVED = b"<html><body>served revision</body></html>"
HTML_PINNED = b"<html><body>pinned revision</body></html>"
OCR_TEXT = "OCR-TEXT-Form-approval-signed"


def _mail() -> bytes:
    message = EmailMessage()
    message["Subject"], message["From"] = "Proof thread", "printer@example.com"
    message.set_content("Mail body sentence 4C2.")
    message.add_attachment(b"%PDF-1.4 attached purchase order", maintype="application",
                           subtype="pdf", filename="PO#1.pdf")
    return bytes(message)


MAIL = _mail()
ATTACHMENT = b"%PDF-1.4 attached purchase order"
NOTE = b"note member text"
EXTRA = 55  # members beyond the verifier's 50-entry page: no row may be lost


def _zip() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("Ex/A/note.txt", NOTE)
        archive.writestr("Ex/B/thread.eml", MAIL)
        for n in range(EXTRA):
            archive.writestr(f"Ex/C/item-{n:02d}.txt", f"member {n}".encode())
    return buffer.getvalue()


ZIP = _zip()


def h(data: bytes) -> str:
    return sha256(data).hexdigest()


def personal(owner: str, name: str) -> str:
    return (f"https://{OD}/personal/{owner}_x_com/Documents/Microsoft%20Teams%20Chat%20Files/"
            + name.replace(" ", "%20"))


MAP_URL = personal("vince", "Lifecycle Map.md")
ASSET_MD = "Asset Tracker — Use Case.md"
ASSET_URL = personal("stephen", "Asset%20Tracker%20%E2%80%94%20Use%20Case.md")
PROCESS = f"https://{SP}/sites/Operations/Shared%20Documents/Owner/process-maps/po-window"


def record() -> dict:
    """A trimmed copy of the source record's shape."""
    teams = {"source_message_url": "https://teams.microsoft.com/l/message/19%3Aabc/1",
             "source_message_id": "1", "source_attachment_id": "d6-1",
             "submitted_at": "2026-10-12T18:20:43.979Z",
             "versions": {"next_link": None, "value": [{"id": "1.0", "size": len(MAP)}]}}
    files = [
        {"kind": "original submitted process map", "owner": "Vince Alton",
         "relative_path": "originals/Lifecycle Map.md", "mime_type": "text/markdown",
         "detected_media_type": "text/plain", "sha256": h(MAP), "size_bytes": len(MAP),
         "source_url": MAP_URL, "claude_full_body_read_status": "not_verified", **teams},
        {"kind": "supplied text export of correspondence", "owner": "Vince Alton",
         "relative_path": "originals/Examples/A/thread (text).txt", "mime_type": "text/plain",
         "detected_media_type": "text/plain", "sha256": h(TEXT_EXPORT),
         "size_bytes": len(TEXT_EXPORT), "drive_id": DRIVE, "item_id": "01TXT",
         "etag": "\"{T},1\"", "linked_from": "Lifecycle Map.md",
         "source_url": f"https://{SP}/sites/PD/Examples/A/thread (text).txt?web=1"},
        {"kind": "explicit case-linked business source file", "owner": "Vince Alton",
         "relative_path": "originals/Examples/F/Form 117 approval.pdf",
         "mime_type": "application/pdf", "detected_media_type": "image/png",
         "sha256": h(PNG), "size_bytes": len(PNG), "drive_id": DRIVE, "item_id": "01FORM",
         "etag": "\"{F},1\"", "linked_from": "Lifecycle Map.md"},
        {"kind": "original submitted process map", "owner": "Stephen Alton",
         "relative_path": f"originals/{ASSET_MD}", "mime_type": "text/markdown",
         "detected_media_type": "text/plain", "sha256": h(b"asset md"), "size_bytes": 8,
         "source_url": ASSET_URL, **teams},
        {"kind": "explicit case-linked business source file", "owner": "Stephen",
         "relative_path": "originals/Assets/Tracker.xlsx", "linked_from": ASSET_MD,
         "mime_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
         "detected_media_type": "application/zip", "sha256": h(WORKBOOK),
         "size_bytes": len(WORKBOOK), "drive_id": DRIVE, "item_id": "01BOOK",
         "etag": "\"{B},892\"", "version_id": "197.0", "modified_at": "2026-10-08T12:35:59Z",
         "historical_reference_version": {"bytes_retrieved": False, "version_id": "195.0",
                                          "modified_at": "2026-10-02T12:53:25Z",
                                          "sha256": None, "size_bytes": 9},
         "note": "Current file differs from the revision named in the narrative."},
        {"kind": "original submitted business evidence archive", "owner": "Vince Alton",
         "relative_path": "originals/examples.zip", "mime_type": "application/zip",
         "detected_media_type": "application/zip", "sha256": h(ZIP), "size_bytes": len(ZIP),
         "drive_id": DRIVE, "item_id": "01ZIP", "version_id": "1.0",
         "source_url": f"https://{SP}/sites/AGENTS/Shared%20Documents/Use%20Cases/examples.zip",
         "archive_member_count": 2 + EXTRA},
        {"kind": "original supplied archive member", "owner": "Vince Alton",
         "relative_path": "originals/examples/Ex/A/note.txt", "archive_member": "Ex/A/note.txt",
         "parent_sha256": h(ZIP), "mime_type": "text/plain",
         "detected_media_type": "text/plain", "sha256": h(NOTE), "size_bytes": len(NOTE)},
        {"kind": "original supplied archive member", "owner": "Vince Alton",
         "relative_path": "originals/examples/Ex/B/thread.eml",
         "archive_member": "Ex/B/thread.eml", "parent_sha256": h(ZIP),
         "mime_type": "message/rfc822", "detected_media_type": "message/rfc822",
         "sha256": h(MAIL), "size_bytes": len(MAIL)},
        {"kind": "attachment embedded in supplied EML", "owner": "Vince Alton",
         "relative_path": "originals/embedded_eml/x/003_PO_1.pdf", "original_name": "PO#1.pdf",
         "mime_part_index": 3, "parent_sha256": h(MAIL), "mime_type": "application/pdf",
         "detected_media_type": "application/pdf", "sha256": h(ATTACHMENT),
         "size_bytes": len(ATTACHMENT)},
        {"kind": "original submitted process map package", "owner": "Owner Name",
         "case": "po-window", "relative_path": "originals/Owner/process-maps/po-window/map.html",
         "mime_type": "text/html", "detected_media_type": "text/html",
         "sha256": h(HTML_PINNED), "size_bytes": len(HTML_PINNED), "drive_id": DRIVE,
         "item_id": "01HTML", "etag": "\"{H},1\"",
         "source_url": f"{PROCESS}/map.html"},
    ]
    files += [{"kind": "original supplied archive member", "owner": "Vince Alton",
               "relative_path": f"originals/examples/Ex/C/item-{n:02d}.txt",
               "archive_member": f"Ex/C/item-{n:02d}.txt", "parent_sha256": h(ZIP),
               "mime_type": "text/plain", "detected_media_type": "text/plain",
               "sha256": h(f"member {n}".encode()), "size_bytes": len(f"member {n}")}
              for n in range(EXTRA)]
    gaps = [{"item": "Screening Bottles", "status": "offered, not submitted"},
            {"item": "Asset tracker source revision", "status": "submission-date revision "
             "not yet recovered"}]
    return {"schema": "plx-advisory/v1", "source_revision": "f" * 64,
            "source_id": "project-input:test", "created_at": "2026-10-14T17:36:00Z",
            "source_inventory": {"counts": {"all_manifest_entries": len(files)},
                                 "files": files, "gaps": gaps},
            "source_inventory_receipt": {"inventory_observed_at": "2026-10-14T17:33:00Z"}}


QRM = b"%PDF-1.4 quality risk record"
SLIP = b"%PDF slip."
MAIL_BODY = "Packing slip attached for the dandy order."
ART_URL = f"https://{SP}/sites/PD/Library/Examples%20F"


def delta() -> dict:
    """A trimmed copy of the incremental record's shape: new pins, an alias of baseline
    bytes, a derived rendering, the case register, historical mail pins and open gaps."""
    pins = [
        {"kind": "original case-linked QRM evidence", "owner": "Owner Name",
         "relative_path": "expanded/originals/risk.pdf", "name": "risk.pdf",
         "mime_type": "application/pdf", "sha256": h(QRM), "size_bytes": len(QRM),
         "drive_id": DRIVE, "item_id": "01QRM", "source_version": "10.0",
         "source_url": f"https://{SP}/sites/QC/Shared%20Documents/risk.pdf"},
        {"kind": "original submitted AO corpus source alias", "owner": "Owner Name",
         "relative_path": "originals/Examples/F/Form 117 approval.pdf", "name": "form.pdf",
         "sha256": h(PNG), "size_bytes": len(PNG), "drive_id": DRIVE, "item_id": "01ALIAS"},
        {"kind": "historical complete rendered artifact text, derived representation",
         "owner": "Vince Alton", "relative_path": "expanded/rendered text.txt",
         "name": "rendered text.txt", "sha256": h(b"rendered"), "size_bytes": 8,
         "repository_source": "https://github.com/org/repo/blob/abc/context/x.json"}]
    register = [
        {"case_id": "case-map", "file_paths": ["originals/Lifecycle Map.md"]},
        {"case_id": "case-assets", "file_paths": [f"originals/{ASSET_MD}"]},
        {"case_id": "case-archive", "file_paths": ["originals/examples.zip",
                                                   "expanded/rendered text.txt"]},
        {"case_id": "case-corpus", "file_paths": ["originals/Examples/F/Form 117 approval.pdf"]}]
    mail = {"status": "historically pinned; current403; bytes not refetched",
            "mail_messages": [{"source_id": "receiving-mail", "message_id": "AAMk1",
                               "received_at": "2026-09-15T20:27:21Z", "body_sha256": "e" * 64}],
            "mail_attachments": [{"source_id": "receiving-pdf", "message": "receiving-mail",
                                  "name": "slip.pdf", "bytes": len(SLIP), "sha256": h(SLIP)}]}
    return {"schema": "plx-advisory/v1", "source_revision": "b" * 64,
            "source_id": "project-input:test", "created_at": "2026-10-14T17:54:30Z",
            "counts": {"all_file_records": len(record()["source_inventory"]["files"])
                       + len(pins)},
            "delta_source_pins": pins, "case_register": register,
            "historical_receiving_source_pins": mail,
            "open_gaps": [{"item": "Screening Bottles", "status": "offered again"},
                          {"item": "Receiving mailbox", "status": "current read 403"}],
            "supersession": "Adds source pins; does not overwrite the earlier record.",
            "source_inventory_receipt": {"observed_at": "2026-10-14T17:49:46Z"}}


CAP_MEMBER = b"capture member text 7Q1"
CAP_SCAN = b"S" * 300  # a member over the (patched) part bound


def _capture_zip() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("Evidence/proof.txt", CAP_MEMBER)
        archive.writestr("Evidence/scan.bin", CAP_SCAN)
    return buffer.getvalue()


CAP_ZIP = _capture_zip()


def capture(name: str, data: bytes, item: str, sha: str | None = None) -> dict:
    """A capture record's shape."""
    return {"schema": "plx-advisory/v1", "kind": "context",
            "created_at": "2026-10-15T10:03:42+00:00",
            "source_revision": "1.0:2026-10-15T09:56:16Z",
            "original_file": {"name": name, "bytes": len(data), "drive_id": DRIVE,
                              "item_id": item, "quickXorHash": quick_xor_hash(data),
                              "modified_at": "2026-10-15T09:56:16Z", "version": "1.0",
                              "sha256": sha, "member_manifest": None,
                              "source_url": f"https://{SP}/sites/AGENTS/Shared%20Documents/"
                                            f"{name}",
                              "body_delivery_status": "pending original read receipt"}}



# -- a manifest record (the read of a SharePoint library: source_manifest,
#    nested_attachment_manifest, zip_member_manifest), trimmed to the record's field shapes
MSITE, MWEB, MLIST = (uuid.UUID(f"{n}{n}{n}{n}0000-0000-4000-8000-00000000000{n}")
                      for n in (1, 2, 3))
MDRIVE = "b!" + base64.urlsafe_b64encode(
    MSITE.bytes_le + MWEB.bytes_le + MLIST.bytes_le).decode().rstrip("=")
ART_WEB = f"https://{SP}/sites/PD/Art%20Examples"
ART_PATH = "/Art Examples"
LOTION = b"# Lotion thread\nThe lotion ends pending 7K2.\n"
INDEX = b"<html><body>index v2 body 3J8</body></html>"
MAP_LINK = "https://claude.ai/artifact/not-a-registry-value"
CAVEATS = {
    "a": "the issue index's lotion re-approval and the customer's concentrate cancellation "
         "are index claims only; the supplied transcripts don't support them (lotion ends "
         "pending; no cancellation in the 142-line concentrate transcript), and neither is "
         "disproven",
    "b": "a full read of a supplied file is not complete email-thread coverage (truncated or "
         "omitted messages, photos and attachments excluded)",
    "c": "B1 approve/pause and B3 lotion share identical bytes, so keep both identities and "
         "count one payload",
    "d": "the 6 embedded DOCX differ in bytes from the same-subject archived DOCX, so keep "
         "both provenances and infer no text change",
    "e": "Form012, Form072A and Form117 examples, lot evidence, damage attachments and the "
         "root-cause report are missing in the source, which is not an access denial"}


def _issues_zip() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(zipfile.ZipInfo("issues/B1.md", (2026, 7, 30, 0, 0, 0)), LOTION)
    return buffer.getvalue()


ISSUES = _issues_zip()


def _pack() -> bytes:
    message = EmailMessage()
    message["Subject"], message["From"] = "Issues pack", "customer@example.com"
    message.set_content("Pack body sentence 2W6.")
    message.add_attachment(ATTACHMENT, maintype="application", subtype="pdf",
                           filename="PO#1.pdf")
    message.add_attachment(ISSUES, maintype="application", subtype="zip",
                           filename="issues.zip")
    return bytes(message)


PACK = _pack()
MFOLDERS = {"01MART": "", "01MAPP": "B Gallons/01 Approve", "01MLOT": "B Gallons/03 Lotion",
            "01MESC": "B Gallons/04 Escalation", "01MBLK": "B Gallons/05 Bulk"}


def unique(item: str) -> str:
    return f"aaaaaaaa-0000-4000-8000-{int(item):012d}"


def source_entry(item: str, folder: str, name: str, data: bytes) -> dict:
    path = f"{folder}/{name}" if folder else name
    return {"canonical_url": f"{ART_WEB}/{quote(path)}", "list_id": str(MLIST),
            "list_item_id": item, "list_item_unique_id": unique(item), "name": name,
            "revision": {"list_etag": f'"{unique(item)},1"',
                         "modified_at": "2026-10-12T14:49:41Z", "version": "1.0"},
            "sha256": h(data), "site_id": f"{SP},{MSITE},{MWEB}", "size_bytes": len(data)}


def bulk(n: int) -> list[tuple[str, str, str, bytes]]:
    return [(str(100 + i), MFOLDERS["01MBLK"], f"bulk-{i:02d}.md", f"bulk {i}".encode())
            for i in range(n)]


def manifest(extra: int = 0) -> dict:
    """The manifest record's shape: four top-level files (two with identical bytes), a mail
    message with a PDF and a zip attachment, the zip's one member (the shared bytes)."""
    sources = [("20", MFOLDERS["01MAPP"], "B1 approve.md", LOTION),
               ("22", MFOLDERS["01MLOT"], "B3 lotion.md", LOTION),
               ("13", MFOLDERS["01MESC"], "issues pack.eml", PACK),
               ("41", "", "00 Index.html", INDEX), *bulk(extra)]
    return {"schema": "plx-advisory/v1", "kind": "context", "advisory_only": True,
            "created_at": "2026-10-17T21:13:01Z", "observed_at": "2026-10-17T21:07:30Z",
            "source_id": "sharepoint:test:original-read-reconciliation",
            "source_revision": "sha256:" + "7" * 64,
            "facts": ["The lotion ends pending 7K2."],
            "scope": {"map_url": MAP_LINK, "top_level_originals": 4 + extra,
                      "unique_top_level_payloads": 3 + extra, "nested_attachments_hashed": 2},
            "source_manifest": [source_entry(*s) for s in sources],
            "nested_attachment_manifest": [
                {"bytes": len(ATTACHMENT), "name": "PO#1.pdf", "parent_list_item_id": "13",
                 "parent_sha256": h(PACK), "sha256": h(ATTACHMENT),
                 "storage_prefix": "__attach_version1.0_#00000000/"},
                {"bytes": len(ISSUES), "name": "issues.zip", "parent_list_item_id": "13",
                 "parent_sha256": h(PACK), "sha256": h(ISSUES),
                 "storage_prefix": "__attach_version1.0_#00000001/"}],
            "zip_member_manifest": [
                {"bytes": len(LOTION), "matching_top_level_list_item_ids": ["22", "20"],
                 "name": "issues/B1.md", "sha256": h(LOTION)}]}


def caveats(record_sha: str, **over) -> dict:
    return {"schema": "plx-case-originals-caveats/v1", "directive": "642494a1e893",
            "advisory_record_sha256": record_sha,
            "caveats": [{"id": k, "text": v} for k, v in CAVEATS.items()], **over}


def proposals(case: str, source: dict) -> bytes:
    return json.dumps({"format": "plx-eval-case-card-proposals/v1", "case": case,
                       "source": source}).encode()

def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t",
                           "-c", "user.email=t@example.com", *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def j(status, value, headers=None):
    return status, dict(headers or {}), json.dumps(value).encode()


class Tenant:
    """Graph over a fixture tenant: drive items (one 403), sharing links (one 404) and one
    watched folder with a registered and a new file. Records every request."""

    FOLDERS = {"01PROC": (DRIVE, "/Owner/process-maps/po-window", "po-window"),
               "01FF": (DRIVE, "/Examples/F", "F")}

    def __init__(self, html=HTML_SERVED):
        self.files = {  # (drive, item): (folder path, folder id, name, bytes)
            (DRIVE, "01TXT"): ("/Examples/A", "01FA", "thread (text).txt", TEXT_EXPORT),
            (DRIVE, "01FORM"): ("/Examples/F", "01FF", "Form 117 approval.pdf", PNG),
            (DRIVE, "01ALIAS"): ("/Corpus", "01FC", "form.pdf", PNG),
            (DRIVE, "01QRM"): ("/QC", "01FQ", "risk.pdf", QRM),
            (DRIVE, "01FNEW"): ("/Examples/F", "01FF", "Form 013B.pdf", PNG + b"new"),
            (DRIVE, "01BOOK"): ("/Assets", "01FS", "Tracker.xlsx", WORKBOOK),
            (DRIVE, "01ZIP"): ("/Use Cases", "01FU", "examples.zip", ZIP),
            (DRIVE, "01HTML"): ("/Owner/process-maps/po-window", "01PROC", "map.html", html),
            (DRIVE, "01NEW"): ("/Owner/process-maps/po-window", "01PROC", "new-map.html", b"<p>new</p>"),
            (PERSONAL, "01MAP"): ("/Microsoft Teams Chat Files", "01FT", "Lifecycle Map.md", MAP)}
        self.shares = {encode_share_id(MAP_URL): (PERSONAL, "01MAP"),
                       encode_share_id(PROCESS): (DRIVE, "01PROC")}
        self.denied = {(DRIVE, "01BOOK")}
        self.ghosts: set[tuple[str, str]] = set()  # listed, then gone (404) when read
        self.packages: set[tuple[str, str]] = set()  # no file facet (a OneNote package)
        self.versions: dict[tuple[str, str, str], bytes] = {}  # historical revisions
        self.etags: dict[tuple[str, str], str] = {}  # an item's eTag, when not the default
        self.mail = None  # a mailbox (the adapter tests' fake) answering /users/ paths
        self.calls: list[tuple[str, str]] = []

    def item(self, drive, item):
        if item in self.FOLDERS:
            _, path, name = self.FOLDERS[item]
            parent = path.rsplit("/", 1)[0] or "/"
            return {"id": item, "name": name, "eTag": "\"{P},1\"", "folder": {"childCount": 2},
                    "parentReference": {"driveId": drive, "id": "01ROOT",
                                        "path": f"/drives/{drive}/root:{parent}"}}
        path, folder, name, data = self.files[(drive, item)]
        etag = self.etags.get((drive, item), f"\"{{{item}}},1\"")
        if (drive, item) in self.packages:
            return {"id": item, "name": name, "eTag": f"\"{{{item}}},1\"", "size": len(data),
                    "package": {"type": "oneNote"},
                    "parentReference": {"driveId": drive, "id": folder,
                                        "path": f"/drives/{drive}/root:{path}"}}
        return {"id": item, "name": name, "eTag": etag, "size": len(data),
                "webUrl": f"https://{SP}/x/{item}",
                "file": {"mimeType": "application/octet-stream",
                         "hashes": {"quickXorHash": quick_xor_hash(data)}},
                "parentReference": {"driveId": drive, "id": folder,
                                    "path": f"/drives/{drive}/root:{path}"}}

    def __call__(self, method, url, headers, body, timeout, max_bytes):
        self.calls.append((method, url))
        parts = urlsplit(url)
        if parts.hostname == "login.microsoftonline.com":
            return j(200, {"access_token": TOKEN, "expires_in": 3599})
        if parts.hostname == SP and parts.path.endswith("download.aspx"):
            query = parse_qs(parts.query)
            if "v" in query:
                return 200, {}, self.versions[(query["d"][0], query["i"][0], query["v"][0])]
            return 200, {}, self.files[(query["d"][0], query["i"][0])][3]
        path = unquote(parts.path)
        if self.mail is not None and (path == "/v1.0/users" or path.startswith("/v1.0/users/")):
            return self.mail(method, url, headers, body, timeout, max_bytes)
        version = re.fullmatch(r"/v1\.0/drives/([^/]+)/items/([^/]+)/versions/([^/]+)/content",
                               path)
        if version:
            if version.groups() not in self.versions:
                return j(404, {"error": {"code": "itemNotFound"}})
            drive, item, number = version.groups()
            return 302, {"Location": f"https://{SP}/_layouts/15/download.aspx?d={drive}&i={item}"
                                     f"&v={number}&tempauth={TEMPAUTH}"}, b""
        shared = re.fullmatch(r"/v1\.0/shares/([^/]+)/driveItem", path)
        if shared:
            if shared.group(1) not in self.shares:
                return j(404, {"error": {"code": "itemNotFound", "message": "Item not found"}})
            drive, item = self.shares[shared.group(1)]
            return j(200, {"id": item, "parentReference": {"driveId": drive}})
        by_path = re.fullmatch(r"/v1\.0/drives/([^/]+)/root:(/.+):", path)
        if by_path:
            folder = next((i for i, f in self.FOLDERS.items()
                           if f[:2] == by_path.groups()), None)
            return (j(200, self.item(by_path.group(1), folder)) if folder
                    else j(404, {"error": {"code": "itemNotFound"}}))
        found = re.fullmatch(r"/v1\.0/drives/([^/]+)/items/([^/]+)(/content|/children)?", path)
        if not found:
            return j(404, {"error": {"code": "itemNotFound"}})
        drive, item, tail = found.groups()
        if (drive, item) in self.denied:
            return j(403, {"error": {"code": "accessDenied", "message": "Access denied"}})
        if (drive, item) in self.ghosts and tail != "/children":
            return j(404, {"error": {"code": "itemNotFound", "message": "Item not found"}})
        if tail == "/content":
            return 302, {"Location": f"https://{SP}/_layouts/15/download.aspx?d={drive}&i={item}"
                                     f"&tempauth={TEMPAUTH}"}, b""
        if tail == "/children":
            return j(200, {"value": [self.item(d, i) for (d, i), f in self.files.items()
                                     if d == drive and f[1] == item]})
        return j(200, self.item(drive, item))


class ReportAssertionTests(unittest.TestCase):
    """DOC-05 / DOC-09 / ING-10 report assertions on synthetic inputs (the live spec's
    own ``expect`` is applied with ``check_expectations``)."""

    def spec(self):
        return json.loads((Path(__file__).resolve().parent.parent / "devloop" / "live-checks"
                           / "case.originals_read.json").read_text(encoding="utf-8"))

    def test_a_cited_fact_without_a_resolving_citation_is_mislabelled(self):
        from operating.cases import FACT_LABEL, INTERPRETATION_LABEL
        good = [{"label": FACT_LABEL, "citation_resolves": True},
                {"label": INTERPRETATION_LABEL, "citation_resolves": False}]
        self.assertEqual(co.audit_findings(good, [])["case_findings"]["mislabelled"], 0)
        for bad in ({"label": FACT_LABEL, "citation_resolves": False},
                    {"label": FACT_LABEL}, {"label": "verified"}, {}):
            self.assertEqual(co.audit_findings([*good, bad], [])["case_findings"]["mislabelled"], 1)

    def test_an_ocr_value_claimed_as_verified_is_counted(self):
        ok = co.audit_findings([], [{"claim": co.MACHINE_READ_LEAD}])["ocr_values"]
        self.assertEqual((ok["derived"], ok["verified_ocr"]), (1, 0))
        bad = co.audit_findings([], [{"claim": co.MACHINE_READ_LEAD}, {"claim": "verified"},
                                     {}])["ocr_values"]
        self.assertEqual((bad["derived"], bad["verified_ocr"]), (3, 2))

    def test_the_spec_fails_a_mislabelled_finding_and_a_verified_ocr_value(self):
        spec = self.spec()
        from operating.cases import FACT_LABEL
        report = {"case_findings": co.audit_findings(
            [{"label": FACT_LABEL, "citation_resolves": False}], [])["case_findings"],
            "ocr_values": co.audit_findings([], [{"claim": "verified"}])["ocr_values"]}
        failures = " ".join(lv.check_expectations(spec, 0, report))
        self.assertIn("case_findings.mislabelled", failures)
        self.assertIn("ocr_values.verified_ocr", failures)

    def entries(self, *statuses):
        rows = {"ok": {"status": "read", "extraction": "complete"},
                "bad": {"status": "unavailable", "http_status": 503, "extraction": "not_run"},
                "unread": {"status": "read", "extraction": "unread"},
                "untyped": {"status": "mystery", "extraction": "not_run"},
                "cut": {"status": "unavailable", "detail": "budget_cut",
                        "extraction": "not_run"}}
        return [dict(rows[s]) for s in statuses]

    def test_a_failing_item_is_typed_and_later_items_are_still_read(self):
        out = co.item_isolation(self.entries("ok", "bad", "ok", "unread", "ok"))
        self.assertEqual((out["failed"], out["typed"], out["untyped"], out["later_read"],
                          out["stalled"], out["outcome"]), (2, 2, 0, 2, 0, "isolated"))

    def test_no_failing_item_is_not_exercised_not_a_pass(self):
        out = co.item_isolation(self.entries("ok", "ok"))
        self.assertEqual((out["failed"], out["outcome"]), (0, "not_exercised"))

    def test_an_untyped_failure_or_a_stall_after_it_is_broken(self):
        untyped = co.item_isolation(self.entries("untyped", "ok"))
        self.assertEqual((untyped["untyped"], untyped["outcome"]), (1, "broken"))
        stalled = co.item_isolation(self.entries("bad", "cut", "cut"))
        self.assertEqual((stalled["stalled"], stalled["outcome"]), (1, "broken"))
        failures = " ".join(lv.check_expectations(self.spec(), 0, {
            "item_isolation": stalled}))
        self.assertIn("item_isolation.stalled", failures)
        failures = " ".join(lv.check_expectations(self.spec(), 0, {"item_isolation": untyped}))
        self.assertIn("item_isolation.untyped", failures)

    def test_a_real_read_renders_cited_facts_and_machine_read_leads_only(self):
        t = CaseOriginalsTest("run_read")
        t.setUp()
        self.addCleanup(t.tmp.cleanup)
        report = t.run_read(Tenant())
        self.assertGreater(report["case_findings"]["rendered"], 0)
        self.assertEqual(report["case_findings"]["mislabelled"], 0)
        self.assertGreater(report["ocr_values"]["derived"], 0)  # the PNG is OCRed
        self.assertEqual(report["ocr_values"]["verified_ocr"], 0)
        self.assertEqual(report["item_isolation"]["untyped"], 0)
        self.assertEqual(report["item_isolation"]["stalled"], 0)


class CaseOriginalsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.raw, self.delta = json.dumps(record()).encode(), json.dumps(delta()).encode()
        self.registry = co.build_registry(
            self.raw, advisory_commit=COMMIT, advisory_path="context/x.json",
            delta_bytes=self.delta, delta_commit="d" * 40, delta_path="context/y.json",
            watch_links=[PROCESS], watch_drives=[(DRIVE, "/Examples/F", ART_URL)])
        self.path = self.root / "case_originals.json"
        self.path.write_text(json.dumps(self.registry), encoding="utf-8")
        self.rows = {row["record_index"]: row for row in self.registry["rows"]
                     if row["status"] == "pinned" and row["origin"] == "baseline"}
        self.delta_rows = [row for row in self.registry["rows"]
                           if row["status"] == "pinned" and row["origin"] == "delta"]

    def run_read(self, tenant, run_id="run-1", **kwargs):
        return co.read(ENV, registry_path=self.path, state_dir=self.root / "state",
                       run_id=run_id, role="verifier", environment="PLX development",
                       sender=tenant, ocr=lambda image, timeout=None: OCR_TEXT, **kwargs)

    def receipt(self, row_id):
        version = h(self.path.read_bytes())
        return json.loads((self.root / "state" / "case-originals" / "receipts" / version
                           / f"{row_id}.json").read_text(encoding="utf-8"))

    def test_registry_rows_gaps_revisions_and_kinds(self):
        registry, rows = self.registry, self.registry["rows"]
        files = record()["source_inventory"]["files"]
        self.assertEqual([(a["role"], a["record_sha256"], a["source_revision"])
                          for a in registry["advisories"]],
                         [("baseline", h(self.raw), "f" * 64), ("delta", h(self.delta), "b" * 64)])
        self.assertEqual(registry["counts"]["file_records"], len(files) + 3)
        current = self.rows[4]
        historical = [r for r in rows if r.get("unfetched_reason") == "historical_revision"]
        self.assertEqual(len(historical), 1)  # 197.0 and 195.0 never merged
        self.assertNotEqual(historical[0]["id"], current["id"])
        self.assertEqual((current["revision"]["version_id"], current["expected"]["sha256"]),
                         ("197.0", h(WORKBOOK)))
        self.assertEqual((historical[0]["revision"]["version_id"],
                          historical[0]["expected"]["sha256"], historical[0]["revision_of"]),
                         ("195.0", None, current["id"]))
        self.assertEqual(self.rows[1]["kind"], "derivative")
        self.assertEqual(self.rows[1]["linked_from"], self.rows[0]["id"])
        self.assertEqual((self.rows[6]["kind"], self.rows[6]["parent"]),
                         ("archive_member", self.rows[5]["id"]))
        self.assertEqual((self.rows[8]["kind"], self.rows[8]["parent"]),
                         ("attachment", self.rows[7]["id"]))
        # cases from the register, inherited by linked files, members and revisions
        self.assertEqual({self.rows[i]["case"] for i in (0, 1)}, {"case-map"})
        self.assertEqual(self.rows[2]["case_ids"], ["case-map", "case-corpus"])
        self.assertEqual({current["case"], historical[0]["case"]}, {"case-assets"})
        self.assertEqual({self.rows[i]["case"] for i in (5, 6, 7, 8)}, {"case-archive"})
        # delta: a new original, an alias of baseline bytes, a derived rendering
        qrm, alias, rendering = self.delta_rows
        self.assertEqual((qrm["kind"], qrm["alias_of"], qrm["case"]), ("original", None, None))
        self.assertEqual((alias["alias_of"], alias["source"]["item_id"]),
                         (self.rows[2]["id"], "01ALIAS"))
        self.assertEqual((rendering["kind"], rendering["case"]), ("derivative", "case-archive"))
        mail = [r for r in rows if r.get("unfetched_reason") == "historical_mail_pin"]
        self.assertEqual([r["kind"] for r in mail], ["mail_message", "attachment"])
        self.assertEqual(mail[1]["parent"], mail[0]["id"])
        self.assertTrue(all(r["exposure"] == "dev-exposed" for r in rows))
        gaps = {(r["gap"]["item"], r["status"]) for r in rows if "gap" in r}
        self.assertEqual(gaps, {("Screening Bottles", "gap"), ("Receiving mailbox", "gap"),
                                ("Screening Bottles", "superseded"),
                                ("Asset tracker source revision", "gap")})
        self.assertEqual({w["folder_url"] for w in registry["watch_folders"]},
                         {PROCESS, ART_URL})
        self.assertNotIn('"originals/', json.dumps(registry))  # no inventory/local paths
        broken = record()
        broken["source_inventory"]["files"][0]["kind"] = "a kind nobody listed"
        with self.assertRaisesRegex(ValueError, "advisory_entry_kind_unknown"):
            co.build_registry(json.dumps(broken).encode(), advisory_commit=COMMIT,
                              advisory_path="context/x.json")

    def test_committed_registry_pins_both_advisory_records_without_bodies(self):
        registry, version = co.load_registry(co.REGISTRY_PATH)
        self.assertEqual([a["record_sha256"] for a in registry["advisories"]],
                         ["5cfdad986c741bb61fd3cde2c21b88c2cc59a608d7522a0494f7c84e360cbe71",
                          "a20ba73959a0c2557d4f343cf1f8108dc3cdababa6a38e537689be58eb332e96",
                          "f2cd062d2ad182dbc8efc6024d89e0aa8b11702275a9b0b22b45e81693181276",
                          # the card-sources input's cited records (Laura's ZIP and
                          # the three R&D maps' rendered text)
                          "b1f2c4fa840b5497d3d0b6b0e61c685997e6593b62dfe0934210b6a25e05e23e",
                          "074d4f122d44c91d448c39c0524705c86cb9a37b77cef87d10322524a8988497",
                          "ab3ce351c65922fed59aec112c00375f87ad97de9dab2fac30aaef2b29623d80",
                          "c8b15fc76d838ec5f67b2314067628b3e1d746e9845d071e3ad54efc8bd7c9cd"])
        self.assertEqual([a["role"] for a in registry["advisories"]][-4:], ["card"] * 4)
        self.assertEqual((registry["counts"]["file_records"], registry["counts"]["aliases"],
                          len(registry["counts"]["cases"])), (92, 21, 11))
        text = co.REGISTRY_PATH.read_text(encoding="utf-8")
        for leak in ('"originals/', '"expanded/', "tempauth", "downloadUrl", "/home/",
                     "Bearer"):
            self.assertNotIn(leak, text)
        revisions = sorted(r["revision"]["version_id"] for r in registry["rows"]
                           if "revision" in r and r["revision"]["version_id"] in
                           ("195.0", "197.0"))
        self.assertEqual(revisions, ["195.0", "197.0"])
        # One row each, bound to their case and gap; over the
        # reader's content bound a typed oversize row, never a sha256 the record lacks
        captures = [r for r in registry["rows"] if r.get("origin") == "capture"]
        self.assertEqual([(r["capture"]["path"][8:20], r["capture"]["gap"], r["status"],
                           r["expected"]["sha256"], (r.get("oversize") or {}).get("bound"))
                          for r in captures],
                         [("5ed665d02673", "gap-4c7890a11a16", "oversize", None, 100_000_000),
                          ("6e920cf5079d", "gap-76d7fe186b32", "oversize", None, 100_000_000),
                          ("fcc0f9365b5a", "gap-4dc93a84fa19", "oversize", None, 100_000_000),
                          ("46b6c69ade81", "gap-16c8ab0aa25a", "unpinned", None, None)])
        self.assertEqual(len({r["case"] for r in captures}), 1)
        self.assertEqual(registry["counts"]["captures"], 4)

    def test_read_verifies_bytes_reports_errors_and_identity(self):
        tenant = Tenant()
        report = self.run_read(tenant)
        pages = report["entry_pages"]
        entries = {k: v for page in pages for k, v in page.items()}
        total = sum(r["status"] in ("pinned", "unfetched") for r in self.registry["rows"])
        self.assertEqual((report["entries"], report["reported"], report["unreported"]),
                         (total, total, 0))
        # every registered entry, the served revision and the 2 discovered files, paged
        self.assertEqual((report["revisions"], report["discovered"]), (1, 2))
        self.assertEqual([len(p) for p in pages], [50, total + 3 - 50])
        self.assertEqual(sum(e["registered"] for e in entries.values()), total)
        self.assertEqual(report["undiagnosed"], 0)
        self.assertTrue(report["ok"])
        status = {i: entries[self.rows[i]["id"]] for i in range(10)}
        self.assertEqual((status[0]["status"], status[0]["source"]), ("read", "shared_link"))
        # PNG bytes registered as a PDF: an image, OCRed, original name and MIME kept
        form = self.receipt(self.rows[2]["id"])
        self.assertEqual((form["body_status"], form["match"], form["detected_type"],
                          form["detected_media_type"], form["provider_mime_type"]),
                         ("read", True, "png", "image/png", "application/pdf"))
        self.assertEqual(form["type_warnings"], ["type_mismatch:pdf"])
        self.assertEqual(form["extraction"]["methods"], ["tesseract-ocr"])
        # Graph 403 / 404 name the status and code; a pin with no route says why
        self.assertEqual((status[4]["status"], status[4]["http_status"], status[4]["error_code"]),
                         ("denied", 403, "access_denied"))
        self.assertEqual((status[3]["status"], status[3]["http_status"], status[3]["error_code"]),
                         ("gone", 404, "item_not_found"))
        # a historical revision reads through its item (denied here, as the item is); a
        # mail pin with no mailbox configured says so
        unfetched = {(row["unfetched_reason"], entries[row["id"]]["status"],
                      entries[row["id"]]["detail"])
                     for row in self.registry["rows"] if row["status"] == "unfetched"}
        self.assertEqual(unfetched, {("historical_revision", "denied", "http_403"),
                                     ("historical_mail_pin", "no_route",
                                      "mailbox_not_configured")})
        self.assertNotIn("unavailable", {e["status"] for e in entries.values()
                                         if e["detail"] != "repository_ref_invalid"})
        # another revision's bytes: the pinned row reads mismatch and keeps its pin; the
        # served bytes are their own revision entry, stored and extracted
        store = self.root / "state" / "case-originals"
        self.assertEqual((status[9]["status"], status[9]["sha256_match"], status[9]["detail"]),
                         ("mismatch", False, "sha256_mismatch"))
        pinned = self.receipt(self.rows[9]["id"])
        self.assertEqual((pinned["expected"]["sha256"], pinned["sha256"], pinned["stored"]),
                         (h(HTML_PINNED), h(HTML_SERVED), None))
        revision = entries[status[9]["current_revision"]]
        self.assertEqual((revision["status"], revision["revision_of"], revision["registered"]),
                         ("revision_read", self.rows[9]["id"], False))
        served = self.receipt(status[9]["current_revision"])
        self.assertEqual((served["sha256"], served["stored"]),
                         (h(HTML_SERVED), f"case-originals/{h(HTML_SERVED)}"))
        self.assertIn("served revision", (self.root / "state" / served["extraction"][
            "text_ref"]).read_text(encoding="utf-8"))
        self.assertEqual(report["problem_pages"][0][self.rows[9]["id"]],
                         "mismatch:-:-:sha256_mismatch:not_run")
        # members and the mail attachment through the archive's one read, bytes in the store
        for i in (5, 6, 7, 8):
            self.assertEqual((status[i]["status"], status[i]["sha256_match"]), ("read", True))
            self.assertTrue((store / self.rows[i]["expected"]["sha256"]).exists())
        self.assertEqual(status[8]["source"], "container")
        downloads = [u for _, u in tenant.calls if "download.aspx" in u]
        self.assertEqual(sum("i=01ZIP" in u for u in downloads), 1)
        self.assertEqual(self.receipt(self.rows[8]["id"])["canonical_source"]["attachment_name"],
                         "PO#1.pdf")
        # delta rows: the new original read, the alias verified at its own source
        qrm, alias, rendering = (entries[r["id"]] for r in self.delta_rows)
        self.assertEqual((qrm["status"], qrm["sha256_match"]), ("read", True))
        self.assertEqual((alias["status"], alias["sha256_match"]), ("read", True))
        self.assertEqual((rendering["status"], rendering["detail"]),  # "abc" is no commit
                         ("unavailable", "repository_ref_invalid"))
        # one receipt per file with the registry version and the reader's identity
        receipt = self.receipt(self.rows[0]["id"])
        self.assertEqual(receipt["registry_version"], h(self.path.read_bytes()))
        self.assertEqual(receipt["reader"], {
            "account_uuid": ENV["CLAUDE_CODE_ACCOUNT_UUID"],
            "remote_session_id": ENV["CLAUDE_CODE_REMOTE_SESSION_ID"],
            "container_id": ENV["CLAUDE_CODE_CONTAINER_ID"], "run_id": "run-1",
            "role": "verifier", "environment": "PLX development"})
        self.assertEqual(report["reader"], receipt["reader"])
        self.assertEqual((receipt["bytes"], receipt["sha256"]), (len(MAP), h(MAP)))
        self.assertTrue((store / h(MAP)).exists())
        text = self.root / "state" / receipt["extraction"]["text_ref"]
        self.assertIn("Unique body sentence 9F3.", text.read_text(encoding="utf-8"))
        # watched folders: by sharing link and by drive id + path (a nondefault library);
        # every file there the registry does not pin is read in full
        folders = {f["folder_url"]: f["id"] for f in self.registry["watch_folders"]}
        watched = {w["id"]: w for w in report["watch_folders"]}
        for url in (PROCESS, ART_URL):
            self.assertEqual({k: watched[folders[url]][k] for k in
                              ("status", "files", "registered", "discovered", "read")},
                             {"status": "listed", "files": 2, "registered": 1, "discovered": 1,
                              "read": 1})
        self.assertEqual((status[9]["watch_folder"], status[2]["watch_folder"]),
                         (folders[PROCESS], folders[ART_URL]))
        found = [json.loads(p.read_text()) for p in (store / "receipts" /
                 "discovered").glob("nr-*.json")]
        self.assertEqual(sorted((r["status"], r["body_status"], r["name"], r["sha256"],
                                 r["watch_folder"]) for r in found),
                         [("discovered", "read", "Form 013B.pdf", h(PNG + b"new"),
                           folders[ART_URL]),
                          ("discovered", "read", "new-map.html", h(b"<p>new</p>"),
                           folders[PROCESS])])
        for receipt in found:
            self.assertEqual(receipt["provider_revision"]["etag_read"],
                             f"\"{{{receipt['canonical_source']['item_id']}}},1\"")
            self.assertTrue((store / receipt["sha256"]).exists())
            self.assertEqual(entries[receipt["entry_id"]]["status"], "discovered_read")
        self.assertEqual(report["by_status"]["discovered_read"], 2)
        self.assertTrue(all(method == "GET" for method, url in tenant.calls
                            if "login.microsoftonline.com" not in url))

    def test_a_row_proven_by_a_recorded_capture_is_stored_and_served(self):
        """A pin an earlier capture proved (``proven_by``) is held like any other read:
        its bytes and extracted text go into the store, so the host can serve it and the
        scorer counts it held. Verified by hash only, it was held nowhere (RRO map)."""
        from operating import captured_originals as captured

        proven = record()
        proven["source_inventory"]["files"][0].update(
            claude_full_body_read_status="proven_by_recorded_capture",
            claude_receipt_id="eval.use_case_proposal",
            source_message_url="https://teams.microsoft.com/l/message/19%3Aabc%40thread.v2/1"
                               "?context=%7b%22contextType%22%3a%22chat%22%7d")
        self.raw = json.dumps(proven).encode()
        registry = self.build(watch_links=[PROCESS])
        self.path.write_text(json.dumps(registry), encoding="utf-8")
        row = next(r for r in registry["rows"] if r.get("expected", {}).get("sha256") == h(MAP)
                   and r["origin"] == "baseline")
        self.assertEqual(row["proven_by"], ["eval.use_case_proposal"])
        self.run_read(Tenant())
        receipt = self.receipt(row["id"])
        self.assertEqual((receipt["body_status"], receipt["match"], receipt["proven_by"],
                          receipt["stored"], receipt["extraction"]["status"]),
                         ("read", True, ["eval.use_case_proposal"],
                          f"case-originals/{h(MAP)}", "complete"))
        store = self.root / "state"
        self.assertEqual(h((store / receipt["stored"]).read_bytes()), h(MAP))
        self.assertIn("Unique body sentence 9F3.",
                      (store / receipt["extraction"]["text_ref"]).read_text(encoding="utf-8"))
        held = captured.CapturedOriginals(store, gate=lambda *_: False).entries()[row["id"]]
        self.assertIsNone(held.reason)
        self.assertEqual({k: held.unit[k] for k in ("kind", "chat_id", "message_id")},
                         {"kind": "teams_chat", "chat_id": "19:abc@thread.v2", "message_id": "1"})

    def test_discovered_reads_survive_a_registry_change_without_a_request(self):
        """A registry v2 (one row added) in the same state dir reuses every discovered read
        of v1 (same eTag, size, intact bytes): no content GET, and the receipt says so."""
        registry = self.build(watch_links=[PROCESS])
        self.path.write_text(json.dumps(registry), encoding="utf-8")
        first = self.run_read(Tenant(), run_id="run-v1", reuse_unchanged=True)
        read = first["by_status"]["discovered_read"]
        self.assertGreater(read, 0)
        registry["rows"].append({"id": "gap-added", "status": "gap"})
        self.path.write_text(json.dumps(registry), encoding="utf-8")
        tenant = Tenant()
        second = self.run_read(tenant, run_id="run-v2", reuse_unchanged=True)
        self.assertEqual((second["discovered_reused"], second["by_status"]["discovered_read"]),
                         (read, read))
        self.assertFalse([u for _, u in tenant.calls
                          if "download.aspx" in u and "i=01NEW&" in u])
        found = json.loads((self.root / "state" / "case-originals" / "receipts" / "discovered"
                            / f"{co._id('nr', f'{DRIVE}/01NEW')}.json").read_text())
        self.assertEqual((found["registry_version"], found["reused"]["basis"]),
                         (h(self.path.read_bytes()), "etag_unchanged"))

    def test_a_transport_failure_and_a_503_are_retried_before_a_file_is_unavailable(self):
        tenant, failures = Tenant(), iter([urllib.error.URLError("reset"), 503])

        def flaky(method, url, headers, body, timeout, max_bytes):
            if urlsplit(url).path.endswith("/items/01NEW/content"):
                failure = next(failures, None)
                if isinstance(failure, Exception):
                    raise failure
                if failure:
                    return j(failure, {"error": {"code": "serviceNotAvailable"}})
            return tenant(method, url, headers, body, timeout, max_bytes)

        slept = []
        report = self.run_read(flaky, run_id="run-retry", sleep=slept.append)
        entry = self.entries(report)[co._id("nr", f"{DRIVE}/01NEW")]
        self.assertEqual(entry["status"], "discovered_read")
        self.assertEqual(len(slept), 2)

    def test_a_watched_folder_graph_refuses_names_status_and_code(self):
        tenant = Tenant()
        tenant.FOLDERS = {"01PROC": Tenant.FOLDERS["01PROC"]}  # the drive path answers 404
        tenant.shares.pop(encode_share_id(PROCESS))
        report = self.run_read(tenant, run_id="run-4")
        self.assertEqual(sorted((w["status"], w["http_status"], w["error_code"])
                                for w in report["watch_folders"]),
                         [("gone", 404, "item_not_found")] * 2)

    def test_summary_carries_no_body_text_secret_or_url(self):
        out = io.StringIO()
        with mock.patch("sys.stdout", out), \
                mock.patch.object(co, "REGISTRY_PATH", self.path):
            code = co.main(["read", "--run-id", "run-2", "--role", "worker"],
                           environ={**ENV, co.STATE_ENV: str(self.root / "state")},
                           sender=Tenant())
        self.assertEqual(code, 0)
        stdout = out.getvalue()
        document = json.loads(stdout)
        # the live spec over this real output: only the registry's size differs here (its
        # fewer gaps are under the cap, never a failure)
        spec = json.loads((co.ROOT / "devloop" / "live-checks" / f"{co.REPORT_CHECK}.json")
                          .read_text(encoding="utf-8"))
        lv.validate_spec(spec, stem=co.REPORT_CHECK)
        self.assertEqual(lv.check_expectations(spec, code, document),
                         ["entries != expected", "captures_reported != expected",
                          f"reported < {spec['expect']['json_min']['reported']}"])
        recorded = lv._recorded(spec, document)
        problems = {k: v for page in recorded["problem_pages[*]"] for k, v in page.items()}
        entries = {k: v for page in document["entry_pages"] for k, v in page.items()}
        # every entry not read is in the recorded detail by id, with its cause token
        self.assertEqual(set(problems), {k for k, e in entries.items()
                                         if e["status"] not in co.READ_STATUSES
                                         or e["sha256_match"] is False})
        self.assertIn("denied:403:access_denied:http_403:not_run", problems.values())
        self.assertEqual(sum(recorded["failure_causes"].values()), len(problems))
        self.assertEqual(recorded["by_status"]["discovered_read"], 2)
        self.assertTrue(any((self.root / "state" / "case-originals" / "receipts").iterdir()))
        # stdout names files (the per-entry list) but carries no body, text, secret or URL;
        # the recorded detail carries ids, enums and counts only
        self.assertIn("Lifecycle Map.md", {e["name"] for e in entries.values()})
        for leak in (SECRET, TOKEN, TEMPAUTH, "Unique body sentence", "Mail body sentence",
                     "member text", "https://", "Vince", "Stephen", "served revision",
                     "new</p>"):
            self.assertNotIn(leak, stdout)
        detail = json.dumps(recorded)
        for leak in ("Lifecycle Map", "PO#1", "Form 013B", "new-map", "https://"):
            self.assertNotIn(leak, detail)

    def test_the_live_floor_over_the_committed_registry_needs_a_graph_read(self):
        # the committed registry through a tenant that serves none of its files: every one of
        # its 154 entries (150, the card-sourced Teams attachment and the three R&D maps)
        # and 4 captures reports why, yet the check fails without a Graph read
        with mock.patch.object(co, "REPOSITORY_ROOT", self.root):
            report = co.read(ENV, registry_path=co.REGISTRY_PATH, state_dir=self.root / "s",
                             run_id="run-floor", role="verifier", sender=Tenant(),
                             ocr=lambda image: OCR_TEXT)
        spec = json.loads((co.ROOT / "devloop" / "live-checks" / f"{co.REPORT_CHECK}.json")
                          .read_text(encoding="utf-8"))
        self.assertEqual((report["entries"], report["unreported"], report["undiagnosed"],
                          report["captures_reported"], report["ok"]), (154, 0, 0, 4, True))
        self.assertEqual(lv.check_expectations(spec, 0, report),
                         ["by_status.read < 1", "read_via.drive_item < 1",
                          "case_findings.rendered < 1"])  # nothing read, nothing rendered
        report["by_status"]["read"] = report["read_via"]["repository"] = 1  # the Git read only
        report["case_findings"]["rendered"] = 1
        self.assertEqual(lv.check_expectations(spec, 0, report), ["read_via.drive_item < 1"])

    def unfetched(self, reason):
        return [r for r in self.registry["rows"] if r.get("unfetched_reason") == reason]

    def test_a_historical_revision_reads_its_named_version(self):
        tenant = Tenant()
        tenant.denied.clear()
        tenant.versions[(DRIVE, "01BOOK", "195.0")] = b"r" * 9  # the pin's 9 bytes
        report = self.run_read(tenant, watch=False)
        entries = {k: v for page in report["entry_pages"] for k, v in page.items()}
        row = self.unfetched("historical_revision")[0]
        self.assertEqual((entries[row["id"]]["status"], entries[row["id"]]["sha256_match"]),
                         ("read", True))
        receipt = self.receipt(row["id"])
        self.assertEqual((receipt["read_via"], receipt["match_basis"], receipt["bytes"]),
                         ("drive_item_version", "bytes", 9))
        self.assertIn(1, [report["read_via"].get("drive_item_version")])
        self.assertTrue(any("/items/01BOOK/versions/195.0/content" in u
                            for _, u in tenant.calls))
        # other bytes under that version: a mismatch, never a read
        tenant.versions[(DRIVE, "01BOOK", "195.0")] = b"r" * 8
        report = self.run_read(tenant, run_id="run-2", watch=False)
        entries = {k: v for page in report["entry_pages"] for k, v in page.items()}
        self.assertEqual((entries[row["id"]]["status"], entries[row["id"]]["detail"]),
                         ("mismatch", "bytes_mismatch"))

    def mailbox(self, *received):
        """The adapter tests' mailbox fake: one message per received time (the first has
        the pinned attachment), each readable by its immutable id."""
        sender = adapters.FakeSender()
        raw = [dict(adapters.message(f"IMM-{n}", f"CK{n}"), receivedDateTime=at,
                    hasAttachments=n == 0) for n, at in enumerate(received)]
        sender.routes[adapters.MSG_PATH] = j(200, {"value": raw})
        for record in raw:
            sender.routes[f"{adapters.MSG_PATH}/{record['id']}"] = j(200, dict(
                record, body={"contentType": "text", "content": MAIL_BODY}))
        sender.routes[f"{adapters.MSG_PATH}/IMM-0/attachments"] = j(200, {"value": [
            {"id": "ATT-1", "name": "slip.pdf", "size": len(SLIP), "isInline": False,
             "@odata.type": "#microsoft.graph.fileAttachment"}]})
        sender.routes[f"{adapters.MSG_PATH}/IMM-0/attachments/ATT-1"] = j(200, {
            "@odata.type": "#microsoft.graph.fileAttachment", "id": "ATT-1",
            "name": "slip.pdf", "size": len(SLIP),
            "contentBytes": __import__("base64").b64encode(SLIP).decode()})
        return sender

    def run_mail(self, *received):
        tenant = Tenant()
        tenant.mail = self.mailbox(*received)
        report = co.read({**ENV, co.MAILBOX_ENV: adapters.MAILBOX}, registry_path=self.path,
                         state_dir=self.root / "state", run_id="run-mail", role="verifier",
                         sender=tenant, ocr=lambda image, timeout=None: OCR_TEXT, watch=False)
        entries = {k: v for page in report["entry_pages"] for k, v in page.items()}
        message, attachment = self.unfetched("historical_mail_pin")
        return report, entries[message["id"]], entries[attachment["id"]], message, attachment

    def test_a_mail_pin_is_located_by_its_received_second_and_its_attachment_verified(self):
        report, message, attachment, row, child = self.run_mail(
            "2026-09-15T20:27:21Z", "2026-09-15T20:27:22Z")
        self.assertEqual((message["status"], message["detail"]),
                         ("read", "located_by_received_at"))
        own = self.receipt(row["id"])
        self.assertEqual(own["match_basis"], "received_at")
        # the message itself was read: its text body stored and extracted, not just listed
        body = MAIL_BODY.encode()
        self.assertEqual((own["bytes"], own["sha256"]), (len(body), h(body)))
        self.assertIsNotNone(own["stored"])
        self.assertIsNotNone(own["extraction"]["text_ref"])
        self.assertEqual((attachment["status"], attachment["sha256_match"]), ("read", True))
        receipt = self.receipt(child["id"])
        self.assertEqual((receipt["read_via"], receipt["sha256"]), ("mail", h(SLIP)))
        self.assertEqual((report["unreported"], report["undiagnosed"]), (0, 0))

    def test_two_messages_in_one_second_resolve_to_the_one_with_the_pinned_body_hash(self):
        body = MAIL_BODY.encode()
        for row in self.registry["rows"]:
            if row.get("unfetched_reason") == "historical_mail_pin" and not row["parent"]:
                row["expected"]["body_sha256"] = h(body)
        self.path.write_text(json.dumps(self.registry), encoding="utf-8")
        tenant = Tenant()
        sender = tenant.mail = self.mailbox("2026-09-15T20:27:21Z", "2026-09-15T20:27:21Z")
        # the later message (IMM-1) has a different body and is listed after the pinned one
        record = json.loads(sender.routes[f"{adapters.MSG_PATH}/IMM-1"][2])
        record["body"]["content"] = "A different note, same subject and sender."
        sender.routes[f"{adapters.MSG_PATH}/IMM-1"] = j(200, record)
        report = co.read({**ENV, co.MAILBOX_ENV: adapters.MAILBOX}, registry_path=self.path,
                         state_dir=self.root / "state", run_id="run-mail", role="verifier",
                         sender=tenant, ocr=lambda image, timeout=None: OCR_TEXT, watch=False)
        entries = {k: v for page in report["entry_pages"] for k, v in page.items()}
        message, attachment = self.unfetched("historical_mail_pin")
        self.assertEqual((entries[message["id"]]["status"], entries[message["id"]]["detail"]),
                         ("read", "located_by_body_sha256"))
        self.assertEqual(self.receipt(message["id"])["sha256"], h(body))
        self.assertEqual(entries[attachment["id"]]["status"], "read")

    def test_a_people_exclusion_block_is_unavailable_by_its_code_never_gone(self):
        # The guard's directory listing answering 404 is no proof the message is gone:
        # the read is refused (fail closed) and reported by the guard's code.
        tenant = Tenant()
        tenant.mail = self.mailbox("2026-09-15T20:27:21Z", "2026-09-15T20:27:22Z")
        tenant.mail.routes["/v1.0/users"] = j(404, {"error": {"code": "ResourceNotFound"}})
        report = co.read({**ENV, co.MAILBOX_ENV: adapters.MAILBOX}, registry_path=self.path,
                         state_dir=self.root / "state", run_id="run-mail", role="verifier",
                         sender=tenant, ocr=lambda image, timeout=None: OCR_TEXT, watch=False)
        entries = {k: v for page in report["entry_pages"] for k, v in page.items()}
        message, _attachment = self.unfetched("historical_mail_pin")
        self.assertEqual((entries[message["id"]]["status"], entries[message["id"]]["detail"]),
                         ("unavailable", "excluded_people_unresolved"))
        self.assertFalse([u for _m, u, *_ in tenant.mail.calls if "/messages" in u])

    def test_a_mail_pin_not_found_or_ambiguous_says_which(self):
        _, message, attachment, _, _ = self.run_mail("2026-09-15T20:27:22Z")
        self.assertEqual({(e["status"], e["detail"]) for e in (message, attachment)},
                         {("gone", "message_not_located")})
        _, message, attachment, _, _ = self.run_mail("2026-09-15T20:27:21Z",
                                                     "2026-09-15T20:27:21.400Z")
        self.assertEqual({(e["status"], e["detail"]) for e in (message, attachment)},
                         {("unavailable", "message_ambiguous")})

    def test_a_malformed_pin_fails_its_own_entry_not_the_run(self):
        for row in self.registry["rows"]:
            if row.get("unfetched_reason") == "historical_mail_pin" and not row["parent"]:
                row["source"]["received_at"] = "not-a-time"
            if row.get("unfetched_reason") == "historical_revision":
                row["revision"]["version_id"] = "1.0/restoreVersion"
        self.path.write_text(json.dumps(self.registry), encoding="utf-8")
        report, message, attachment, _, _ = self.run_mail("2026-09-15T20:27:21Z")
        entries = {k: v for page in report["entry_pages"] for k, v in page.items()}
        self.assertEqual({message["status"], attachment["status"]}, {"unavailable"})
        for row in self.unfetched("historical_revision"):
            self.assertNotEqual(entries[row["id"]]["status"], "read")
        self.assertEqual((report["unreported"], report["undiagnosed"]), (0, 0))

    def test_a_member_over_the_reader_bound_is_capped_not_lost(self):
        with mock.patch.object(co, "MAX_PART_BYTES", len(MAIL) - 1):
            report = self.run_read(Tenant(), run_id="run-3", watch=False)
        entries = {k: v for page in report["entry_pages"] for k, v in page.items()}
        self.assertEqual(entries[self.rows[7]["id"]]["status"], "capped")
        self.assertEqual(entries[self.rows[8]["id"]]["status"], "unavailable")
        self.assertEqual(entries[self.rows[6]["id"]]["status"], "read")
        self.assertEqual(report["unreported"], 0)

    def test_a_spent_budget_stops_early_and_reports_budget_cut(self):
        pinned = sum(r["status"] == "pinned" and not r["parent"]
                     or r.get("unfetched_reason") == "historical_revision"
                     for r in self.registry["rows"])
        ticks = iter(range(0, 1000, 10))  # each clock read is 10 s later
        report = self.run_read(Tenant(), run_id="run-5", budget_seconds=25,
                               monotonic=lambda: next(ticks))
        self.assertTrue(report["budget_cut"])
        self.assertFalse(report["ok"])
        # the clock is read before each original (0+10) and before its download (20):
        # the first original is read, the second is not started at 30
        self.assertEqual((report["originals_reached"], report["originals_unreached"]),
                         (1, pinned - 1))
        self.assertEqual(report["watch_folders_unreached"], 2)
        self.assertEqual({w["detail"] for w in report["watch_folders"]}, {"budget_cut"})
        entries = {k: v for page in report["entry_pages"] for k, v in page.items()}
        self.assertEqual(report["unreported"], 0)  # a cut entry is reported, never lost
        self.assertEqual(report["undiagnosed"], 0)
        self.assertEqual(entries[self.rows[0]["id"]]["status"], "read")
        cut = [e for e in entries.values() if e["detail"] == "budget_cut"]
        self.assertTrue(cut)
        self.assertTrue(all(e["status"] == "unavailable" for e in cut))
        # the CLI: a typed JSON result and exit 1, not a kill
        out = io.StringIO()
        ticks = iter(range(0, 1000, 10))
        with mock.patch("sys.stdout", out), mock.patch.object(co, "REGISTRY_PATH", self.path), \
                mock.patch.object(co.time, "monotonic", lambda: next(ticks)):
            code = co.main(["read", "--run-id", "run-6", "--role", "worker",
                            "--budget-seconds", "25"],
                           environ={**ENV, co.STATE_ENV: str(self.root / "state")},
                           sender=Tenant())
        self.assertEqual((code, json.loads(out.getvalue())["budget_cut"]), (1, True))

    def test_members_of_a_cut_original_report_budget_cut(self):
        report = self.run_read(Tenant(), run_id="run-mc", budget_seconds=0, watch=False)
        entries = self.entries(report)
        members = [self.rows[7]["id"], self.rows[8]["id"]]
        self.assertEqual({(entries[m]["status"], entries[m]["detail"]) for m in members},
                         {("unavailable", "budget_cut")})
        self.assertFalse([e for e in entries.values()
                          if (e["detail"] or "").startswith("container_")])

    def test_a_deadline_passed_mid_read_starts_no_download(self):
        # the deadline passes after an original's metadata GET: its download never starts,
        # it reports budget_cut, and no later original or folder is read
        tenant = Tenant()
        report = self.run_read(tenant, run_id="run-dl", budget_seconds=60, monotonic=lambda: (
            1000 if any(re.search(r"/items/[^/?]+$", urlsplit(u).path) for _, u in tenant.calls)
            else 0))
        self.assertFalse([u for _, u in tenant.calls
                          if "/content" in u or "download.aspx" in u])
        entries = self.entries(report)
        cut = [e for e in entries.values() if e["detail"] == "budget_cut"]
        self.assertTrue(all(e["status"] == "unavailable" for e in cut))
        self.assertEqual((report["budget_cut"], report["ok"], report["undiagnosed"],
                          report["unreported"]), (True, False, 0, 0))
        self.assertEqual(report["originals_cut_in_read"], 1)
        self.assertEqual(report["by_status"]["read"], sum(  # only the local Git read
            e["status"] == "read" and e["source"] == "repository" for e in entries.values()))

    def test_ocr_gets_the_remaining_budget_and_stops_at_the_deadline(self):
        timeouts = []
        clock = [0.0]

        def ocr(image, timeout=None):
            timeouts.append(timeout)
            clock[0] = 1000.0  # the first image spends the budget
            return OCR_TEXT

        report = co.read(ENV, registry_path=self.path, state_dir=self.root / "state",
                         run_id="run-ocr", role="verifier", sender=Tenant(), ocr=ocr,
                         budget_seconds=45, monotonic=lambda: clock[0], watch=False)
        self.assertEqual(timeouts, [45])  # min(the per-image bound, the budget left)
        self.assertTrue(report["budget_cut"])

    def test_an_ocr_cut_alone_by_the_deadline_still_reports_budget_cut(self):
        late = [False]  # the deadline is seen passed once: right after the first OCR

        def ocr(image, timeout=None):
            if not late[0] and not calls:
                late[0] = True
                calls.append(1)
                raise ValueError("ocr_timeout")
            return OCR_TEXT

        def monotonic():
            if late[0]:
                late[0] = False
                return 1000.0
            return 0.0

        calls: list[int] = []
        report = co.read(ENV, registry_path=self.path, state_dir=self.root / "state",
                         run_id="run-ocr-last", role="verifier", sender=Tenant(), ocr=ocr,
                         budget_seconds=45, monotonic=monotonic, watch=False)
        self.assertEqual((report["ocr_cut"], report["originals_unreached"],
                          report["originals_cut_in_read"]), (1, 0, 0))
        self.assertTrue(report["budget_cut"])  # an image unread for budget is a cut
        self.assertFalse(report["ok"])

    def test_the_spec_timeout_covers_the_budget_and_one_bounded_step(self):
        spec = json.loads((co.ROOT / "devloop" / "live-checks" / f"{co.REPORT_CHECK}.json")
                          .read_text(encoding="utf-8"))
        budget = float(spec["command"][spec["command"].index("--budget-seconds") + 1])
        self.assertGreater(spec["timeout_seconds"], budget + co.STEP_BOUND_SECONDS)

    def test_an_ample_budget_reads_everything_and_reports_no_cut(self):
        report = self.run_read(Tenant(), run_id="run-7", budget_seconds=3600)
        self.assertEqual((report["budget_cut"], report["originals_unreached"],
                          report["watch_folders_unreached"]), (False, 0, 0))
        self.assertTrue(report["ok"])


    def build(self, delta_record=None, **kwargs):
        return co.build_registry(
            self.raw, advisory_commit=COMMIT, advisory_path="context/x.json",
            delta_bytes=json.dumps(delta_record or delta()).encode(), delta_commit="d" * 40,
            delta_path="context/y.json", **kwargs)

    def entries(self, report):
        return {k: v for page in report["entry_pages"] for k, v in page.items()}

    def test_capture_archives_oversize_unpinned_and_pinned_from_the_full_read(self):
        big = b"PK\x03\x04" + b"\0" * (len(CAP_ZIP) + 100)
        tenant = Tenant()
        tenant.files[(DRIVE, "01CAP")] = ("/Use Cases", "01FU", "Evidence_part4.zip", CAP_ZIP)
        tenant.files[(DRIVE, "01BIG")] = ("/Use Cases", "01FU", "Evidence_part1.zip", big)
        files = {"big": capture("Evidence_part1.zip", big, "01BIG"),
                 "cap": capture("Evidence_part4.zip", CAP_ZIP, "01CAP")}
        for key, value in files.items():
            (self.root / f"{key}.json").write_bytes(json.dumps(value).encode())
        args = [("case-art", "gap-big", "a" * 40, "context/big.json"),
                ("case-art", "gap-cap", "b" * 40, "context/cap.json")]
        captures = [(c, g, (self.root / f"{k}.json").read_bytes(), m, p)
                    for (c, g, m, p), k in zip(args, files)]
        bound = len(CAP_ZIP) + 10
        with mock.patch("operating.use_case_intake.MAX_CONTENT_BYTES", bound), \
                mock.patch.object(co, "MAX_PART_BYTES", len(CAP_SCAN) - 1):
            registry = self.build(captures=captures)
            rows = {r["capture"]["gap"]: r for r in registry["rows"] if r.get("capture")}
            over, open_ = rows["gap-big"], rows["gap-cap"]
            self.assertEqual((over["status"], over["oversize"], over["expected"]),
                             ("oversize", {"bytes": len(big), "bound": bound,
                                           "bound_name": "use_case_intake.MAX_CONTENT_BYTES"},
                              {"bytes": len(big), "sha256": None}))
            self.assertEqual((open_["status"], open_["expected"], open_["member_manifest"]),
                             ("unpinned", {"bytes": len(CAP_ZIP), "sha256": None}, None))
            self.assertEqual({(r["case"], tuple(r["case_ids"])) for r in (over, open_)},
                             {("case-art", ("case-art",))})
            self.assertEqual(open_["capture"]["record_sha256"], h(captures[1][2]))
            self.assertEqual((registry["counts"]["captures"], registry["counts"]["oversize"],
                              registry["counts"]["unpinned"]), (2, 1, 1))
            self.path.write_text(json.dumps(registry), encoding="utf-8")
            report = self.run_read(tenant, run_id="run-c1", watch=False)
            entries = self.entries(report)
            # over the content bound: capped from its metadata, never downloaded
            self.assertEqual((entries[over["id"]]["status"], entries[over["id"]]["detail"]),
                             ("capped", "exceeds_cap"))
            self.assertFalse([u for _, u in tenant.calls if "i=01BIG" in u])
            # under it: read in full, matched on size and quickXorHash, manifest by bytes
            self.assertEqual((entries[open_["id"]]["status"], entries[open_["id"]]["sha256_match"],
                              entries[open_["id"]]["match_basis"]),
                             ("read", True, "quick_xor_hash"))
            receipt = self.receipt(open_["id"])
            self.assertEqual(receipt["sha256"], h(CAP_ZIP))
            self.assertEqual([(m["path"], m["name"], m["bytes"], m["sha256"], m["status"],
                               m.get("bound")) for m in receipt["member_manifest"]["members"]],
                             [("Evidence/proof.txt", "proof.txt", len(CAP_MEMBER), h(CAP_MEMBER),
                               "read", None),
                              ("Evidence/scan.bin", "scan.bin", len(CAP_SCAN), None, "oversize",
                               len(CAP_SCAN) - 1)])
            self.assertEqual(report["captures"][open_["id"]],
                             {"status": "read", "registry_status": "unpinned",
                              "bytes": len(CAP_ZIP), "match": True, "members": 1,
                              "member_oversize": 1, "member_unread": 0, "detail": None})
            self.assertEqual((report["captures"][over["id"]]["status"],
                              report["captures_reported"]), ("capped", 2))
            # regenerated with the receipts (CLI): the SHA-256 and members come from the read
            receipts = self.root / "state" / "case-originals" / "receipts" / h(
                self.path.read_bytes())
            (self.root / "base.json").write_bytes(self.raw)
            (self.root / "delta.json").write_bytes(self.delta)
            out = self.root / "pinned.json"
            with mock.patch("sys.stdout", io.StringIO()):
                code = co.main(["registry", "--from", str(self.root / "base.json"),
                                "--advisory-commit", COMMIT, "--advisory-path", "context/x.json",
                                "--delta", str(self.root / "delta.json"), "--delta-commit",
                                "d" * 40, "--delta-path", "context/y.json",
                                *(x for (c, g, m, p), k in zip(args, files) for x in (
                                    "--capture", c, g, m, p, str(self.root / f"{k}.json"))),
                                "--pin-receipts", str(receipts), "--out", str(out)])
            self.assertEqual(code, 0)
            pinned = json.loads(out.read_text(encoding="utf-8"))
            self.assertIn("--pin-receipts", pinned["generated_by"])
            row = next(r for r in pinned["rows"] if r["id"] == open_["id"])
            self.assertEqual((row["status"], row["expected"], row["pinned_from"]["run_id"],
                              row["member_manifest"]["counts"]),
                             ("pinned", {"bytes": len(CAP_ZIP), "sha256": h(CAP_ZIP)}, "run-c1",
                              {"read": 1, "oversize": 1, "unread": 0}))
            members = [r for r in pinned["rows"] if r.get("parent") == open_["id"]]
            self.assertEqual([(m["status"], m["kind"], m["member_path"], m["case"],
                               m["expected"]["sha256"]) for m in members],
                             [("pinned", "archive_member", "Evidence/proof.txt", "case-art",
                               h(CAP_MEMBER)),
                              ("oversize", "archive_member", "Evidence/scan.bin", "case-art",
                               None)])
            self.assertEqual(members[1]["oversize"]["bound"], len(CAP_SCAN) - 1)
            self.assertEqual(next(r for r in pinned["rows"] if r["id"] == over["id"])["status"],
                             "oversize")
            # the pinned registry re-read: members verified through the archive's one read
            self.path.write_text(json.dumps(pinned), encoding="utf-8")
            again = self.entries(self.run_read(tenant, run_id="run-c2", watch=False))
            self.assertEqual((again[open_["id"]]["status"], again[open_["id"]]["match_basis"]),
                             ("read", "sha256"))
            self.assertEqual((again[members[0]["id"]]["status"],
                              again[members[0]["id"]]["sha256_match"],
                              again[members[0]["id"]]["source"]), ("read", True, "container"))
            self.assertEqual((again[members[1]["id"]]["status"],
                              again[members[1]["id"]]["detail"]), ("capped", "exceeds_cap"))
            # a receipt whose read did not verify pins nothing
            stored = [json.loads(f.read_bytes()) for f in sorted(receipts.glob("*.json"))]
            for receipt in stored:
                if receipt["entry_id"] == open_["id"]:
                    receipt["match"] = False
            with self.assertRaisesRegex(ValueError, "capture_receipt_unverified"):
                self.build(captures=captures, receipts=stored)
            # the archive changed at its source: mismatch on the quickXorHash, the served
            # bytes kept as their own revision receipt
            self.path.write_text(json.dumps(registry), encoding="utf-8")
            tenant.files[(DRIVE, "01CAP")] = ("/Use Cases", "01FU", "Evidence_part4.zip",
                                              CAP_ZIP + b"tail")
            changed = self.entries(self.run_read(tenant, run_id="run-c3", watch=False))
        entry = changed[open_["id"]]
        self.assertEqual((entry["status"], entry["detail"], entry["sha256_match"]),
                         ("mismatch", "quick_xor_hash_mismatch", False))
        self.assertEqual(changed[entry["current_revision"]]["revision_of"], open_["id"])

    def test_an_unknown_capture_input_fails_closed(self):
        good = capture("x.zip", b"PK", "01X")

        def attempt(record=good, case="case-art", gap="gap-1", commit="a" * 40):
            raw = record if isinstance(record, bytes) else json.dumps(record).encode()
            return self.build(captures=[(case, gap, raw, commit, "context/c.json")])

        self.assertEqual(attempt()["counts"]["captures"], 1)
        original = good["original_file"]
        refused = {
            "capture_record_invalid": [{**good, "schema": "plx-advisory/v2"},
                                       {k: v for k, v in good.items() if k != "original_file"},
                                       [good]],
            "capture_original_invalid": [{**good, "original_file": {**original, **change}}
                                         for change in ({"bytes": "2"}, {"bytes": -1},
                                                        {"sha256": "not-hex"},
                                                        {"source_url": "http://x"},
                                                        {"drive_id": None}, {"name": None})],
            "capture_duplicates_row": [{**good, "original_file": {**original,
                                                                  "item_id": "01ZIP"}}]}
        for reason, records in refused.items():
            for record in records:
                with self.subTest(reason=reason, record=record), \
                        self.assertRaisesRegex(ValueError, reason):
                    attempt(record)
        for kwargs, reason in (({"case": "case art"}, "capture_case_or_gap_invalid"),
                               ({"gap": ""}, "capture_case_or_gap_invalid"),
                               ({"commit": "abc"}, "capture_commit_invalid")):
            with self.subTest(kwargs=kwargs), self.assertRaisesRegex(ValueError, reason):
                attempt(**kwargs)
        with self.assertRaises(ValueError):
            attempt(b"not json")
        # a registry row the reader cannot account for is refused before any read
        registry = attempt(capture("x.zip", b"P" * 10, "01X"))
        row = next(r for r in registry["rows"] if r.get("capture"))
        for change, reason in (({"status": "oversize"}, "registry_row_oversize_invalid"),
                               ({"expected": {"bytes": None, "sha256": None}},
                                "registry_row_capture_invalid"),
                               ({"status": "pending"}, "registry_row_invalid")):
            broken = {**registry, "rows": [{**r, **change} if r is row else r
                                           for r in registry["rows"]]}
            self.path.write_text(json.dumps(broken), encoding="utf-8")
            with self.subTest(reason=reason), self.assertRaisesRegex(ValueError, reason):
                co.load_registry(self.path)

    def test_the_repository_derivative_is_read_from_the_local_object_store(self):
        repo = self.root / "repo"
        (repo / "context").mkdir(parents=True)
        rendered = "Rendered map text kept inside a record 5D1.\n"
        (repo / "context" / "r.json").write_text(json.dumps(
            {"schema": "plx-advisory/v1", "facts": [{"extracted_text": rendered}]}))
        git(repo, "init", "-q")
        git(repo, "add", ".")
        git(repo, "commit", "-qm", "record")
        commit = git(repo, "rev-parse", "HEAD")
        for ref, status, detail in ((commit, "read", None),
                                    ("0" * 40, "unavailable", "repository_object_absent")):
            changed = delta()
            changed["delta_source_pins"][2].update(
                sha256=h(rendered.encode()), size_bytes=len(rendered.encode()),
                repository_source=f"https://github.com/org/repo/blob/{ref}/context/r.json")
            registry = self.build(changed)
            self.path.write_text(json.dumps(registry), encoding="utf-8")
            row = next(r for r in registry["rows"] if r["source"].get("repository_ref"))
            with mock.patch.object(co, "REPOSITORY_ROOT", repo):
                entry = self.entries(self.run_read(Tenant(), run_id="run-r", watch=False))[
                    row["id"]]
            self.assertEqual((entry["status"], entry["detail"]), (status, detail))
        self.assertEqual(entry["sha256_match"], None)
        receipt = self.receipt(row["id"])
        self.assertEqual(receipt["read_via"], "repository")
        # the readable ref: the record's text field whose bytes carry the registered hash
        self.path.write_text(json.dumps(self.build(changed | {"delta_source_pins": [
            *changed["delta_source_pins"][:2], {**changed["delta_source_pins"][2],
                                                "repository_source": "https://github.com/"
                                                f"org/repo/blob/{commit}/context/r.json"}]})),
            encoding="utf-8")
        with mock.patch.object(co, "REPOSITORY_ROOT", repo):
            self.run_read(Tenant(), run_id="run-r2", watch=False)
        receipt = self.receipt(row["id"])
        self.assertEqual((receipt["body_status"], receipt["match"], receipt["read_via"],
                          receipt["sha256"]),
                         ("read", True, "repository_field", h(rendered.encode())))
        self.assertIn("5D1", (self.root / "state" / receipt["extraction"]["text_ref"])
                      .read_text(encoding="utf-8"))

    def test_every_watch_folder_file_is_read_once_or_named(self):
        """N = 1, 10, 60 new files in a folder watched twice (by link and by drive path):
        each read in full exactly once, a listed file that is gone named 404, a package
        not_file; no entry lost or duplicated, a constant number of requests per file."""
        process = (DRIVE, "/Owner/process-maps/po-window", PROCESS)
        registry = self.build(watch_links=[PROCESS],
                              watch_drives=[(DRIVE, "/Examples/F", ART_URL), process])
        self.path.write_text(json.dumps(registry), encoding="utf-8")
        link, _, by_path = (f["id"] for f in registry["watch_folders"])
        sent = {}
        for n in (1, 10, 60):
            tenant = Tenant()
            for i in range(n):
                tenant.files[(DRIVE, f"01N{i:02d}")] = (
                    process[1], "01PROC", f"added-{i:02d}.txt", f"added {i}".encode())
            tenant.files[(DRIVE, "01GHOST")] = (process[1], "01PROC", "ghost.txt", b"ghost")
            tenant.files[(DRIVE, "01PKG")] = (process[1], "01PROC", "Notebook", b"")
            tenant.ghosts.add((DRIVE, "01GHOST"))
            tenant.packages.add((DRIVE, "01PKG"))
            report = self.run_read(tenant, run_id=f"run-w{n}")
            watched = {w["id"]: w for w in report["watch_folders"]}
            listed = n + 3  # the n new files, new-map.html, the registered map.html, ghost
            self.assertEqual({k: watched[link][k] for k in ("files", "other", "registered",
                                                            "discovered", "read", "repeated")},
                             {"files": listed, "other": 1, "registered": 1,
                              "discovered": listed, "read": n + 1, "repeated": 0})
            self.assertEqual({k: watched[by_path][k] for k in ("files", "registered",
                                                               "discovered", "repeated")},
                             {"files": listed, "registered": 1, "discovered": 0,
                              "repeated": listed})
            downloads = [u for _, u in tenant.calls if "download.aspx" in u]
            for i in range(n):
                self.assertEqual(sum(f"i=01N{i:02d}&" in u for u in downloads), 1)
            pages = report["entry_pages"]
            entries = self.entries(report)
            self.assertTrue(all(len(p) <= 50 for p in pages))
            self.assertEqual(sum(len(p) for p in pages), len(entries))  # none duplicated
            self.assertEqual(len(entries), report["reported"] + report["revisions"]
                             + report["discovered"])
            self.assertEqual(report["discovered"], n + 4)  # + Form 013B in the art folder
            ghost = entries[co._id("nr", f"{DRIVE}/01GHOST")]
            self.assertEqual((ghost["status"], ghost["http_status"], ghost["error_code"]),
                             ("discovered_gone", 404, "item_not_found"))
            package = entries[co._id("nr", f"{DRIVE}/01PKG")]
            self.assertEqual((package["status"], package["detail"]),
                             ("discovered_not_file", "no_file_facet"))
            self.assertEqual(report["by_status"]["discovered_read"], n + 2)
            self.assertTrue(report["ok"])
            sent[n] = report["requests_sent"]
        self.assertEqual((sent[60] - sent[10]) * 9, (sent[10] - sent[1]) * 50)
        # a budget spent while a folder is read: the files not reached are named, never lost
        tenant = Tenant()
        report = self.run_read(tenant, run_id="run-wb", budget_seconds=10, monotonic=lambda: (
            1000 if any("/children" in u for _, u in tenant.calls) else 0))
        entries = self.entries(report)
        cut = [e for e in entries.values() if e["detail"] == "budget_cut"
               and not e["registered"]]
        self.assertEqual((report["budget_cut"], report["ok"], report["discovered_unreached"],
                          len(cut)), (True, False, 1, 1))
        self.assertEqual(cut[0]["status"], "discovered_unavailable")
        self.assertFalse([u for _, u in tenant.calls if "i=01NEW&" in u])

    def test_a_folder_larger_than_one_budget_is_read_across_runs(self):
        """61 files (60 new) and a budget for 22 downloads: the run is a continuation (ok, 39
        pending), the next reuses the 22 and reads 22 more least-recently-read first, each
        file is downloaded once in all, and a run that reads nothing new while files are
        pending fails. A cut registry entry stays a cut (test_a_spent_budget_...)."""
        process = (DRIVE, "/Owner/process-maps/po-window", PROCESS)
        self.path.write_text(json.dumps(self.build(watch_links=[PROCESS])), encoding="utf-8")
        mine = re.compile(r"i=01(D\d\d|NEW)&")  # the discovered files, not the registry's

        def run(run_id, limit, etags=()):
            tenant, listed = Tenant(), [False]
            for i in range(60):
                tenant.files[(DRIVE, f"01D{i:02d}")] = (process[1], "01PROC",
                                                        f"added-{i:02d}.txt", f"added {i}".encode())
            tenant.etags.update({(DRIVE, f"01D{i:02d}"): "v2" for i in etags})
            original = co._Run.read_discovered

            def listing_done(run_, stop):  # the folders are listed; the discovered reads start
                listed[0] = True
                return original(run_, stop)

            def clock():
                downloads = sum(bool(mine.search(u)) for _, u in tenant.calls
                                if "download.aspx" in u)
                return 1000 if listed[0] and downloads >= limit else 0
            with mock.patch.object(co._Run, "read_discovered", listing_done):
                report = self.run_read(tenant, run_id=run_id, budget_seconds=60,
                                       monotonic=clock, reuse_unchanged=True)
            return report, {m.group(1) for _, u in tenant.calls if "download.aspx" in u
                            and (m := mine.search(u))}

        first, got_first = run("run-c1", 22)
        self.assertEqual((first["ok"], first["budget_cut"], first["discovered"],
                          first["discovered_reused"], first["discovered_pending"]),
                         (True, False, 61, 0, 39))
        self.assertEqual(len(got_first), 22)
        # a file the first run read has changed since: it is not first in line again
        changed = int(sorted(g for g in got_first if g.startswith("D"))[0][1:])
        second, got_second = run("run-c2", 22, etags=[changed])
        self.assertEqual((second["ok"], second["discovered_reused"],
                          second["discovered_pending"]), (True, 21, 18))
        self.assertTrue(got_second.isdisjoint(got_first - {f"D{changed:02d}"}))
        self.assertNotIn(f"D{changed:02d}", got_second)
        stalled, got_stalled = run("run-c3", 0)
        self.assertEqual((stalled["ok"], stalled["budget_cut"], got_stalled),
                         (False, False, set()))
        self.assertEqual(stalled["discovered_pending"], 61 - stalled["discovered_reused"])
        done, got_done = run("run-c4", 100)
        self.assertEqual((done["ok"], done["discovered_pending"]), (True, 0))
        self.assertEqual(done["by_status"]["discovered_read"], 61)

    # -- manifest records (source / nested attachment / zip member manifests)
    def with_manifest(self, record=None, *, case="case-art", commit="e" * 40, notes=True,
                      **kwargs):
        raw = record if isinstance(record, bytes) else json.dumps(
            record or manifest()).encode()
        kwargs.setdefault("watch_drives", [(DRIVE, "/Examples/F", ART_URL),
                                           (MDRIVE, ART_PATH, ART_WEB)])
        kwargs.setdefault("watch_links", [PROCESS])
        if notes:
            kwargs.setdefault("caveats", [json.dumps(caveats(h(raw))).encode()])
        return self.build(manifests=[(case, raw, commit, "context/m.json")], **kwargs)

    def manifest_tenant(self, extra=0):
        tenant = Tenant()
        tenant.FOLDERS = {**Tenant.FOLDERS, **{
            folder: (MDRIVE, ART_PATH + (f"/{path}" if path else ""),
                     (path or ART_PATH).rsplit("/", 1)[-1])
            for folder, path in MFOLDERS.items()}}
        files = [("20", "01MAPP", "B1 approve.md", LOTION),
                 ("22", "01MLOT", "B3 lotion.md", LOTION),
                 ("13", "01MESC", "issues pack.eml", PACK),
                 ("41", "01MART", "00 Index.html", INDEX),
                 *((item, "01MBLK", name, data) for item, _, name, data in bulk(extra))]
        for item, folder, name, data in files:
            key = (MDRIVE, f"01MI{item}")
            tenant.files[key] = (tenant.FOLDERS[folder][1], folder, name, data)
            tenant.etags[key] = f'"{{{unique(item).upper()}}},3"'
        return tenant

    def test_manifest_sections_become_rows_with_parents_aliases_and_one_payload(self):
        raw = json.dumps(manifest()).encode()
        registry = self.with_manifest(raw)
        rows = registry["rows"]
        added = [r for r in rows if r.get("origin") == "manifest"]
        # no existing row changes identity or content; the new rows come before the gaps
        before = {r["id"]: r for r in self.build(
            watch_links=[PROCESS], watch_drives=[(DRIVE, "/Examples/F", ART_URL),
                                                 (MDRIVE, ART_PATH, ART_WEB)])["rows"]}
        self.assertEqual({r["id"]: r for r in rows if r["id"] in before}, before)
        self.assertEqual(len(rows), len(before) + 7)
        self.assertEqual([r["status"] for r in rows[rows.index(added[-1]) + 1:]],
                         [r["status"] for r in before.values() if "gap" in r])
        self.assertEqual([(r["record_section"], r["kind"], r["status"], r["name"])
                          for r in added],
                         [("source_manifest", "original", "pinned", "B1 approve.md"),
                          ("source_manifest", "original", "pinned", "B3 lotion.md"),
                          ("source_manifest", "original", "pinned", "issues pack.eml"),
                          ("source_manifest", "original", "pinned", "00 Index.html"),
                          ("nested_attachment_manifest", "attachment", "pinned", "PO#1.pdf"),
                          ("nested_attachment_manifest", "attachment", "pinned", "issues.zip"),
                          ("zip_member_manifest", "archive_member", "pinned",
                           "issues/B1.md")])
        b1, b3, pack, index, po, issues, member = added
        # identity: the library's own ids; the route: the watched drive the list lives in
        self.assertEqual(b1["source"]["list_item_unique_id"], unique("20"))
        self.assertEqual((b1["source"]["drive_id"], b1["source"]["drive_path"],
                          b1["source"]["item_id"], b1["source"]["url"]),
                         (MDRIVE, f"{ART_PATH}/B Gallons/01 Approve/B1 approve.md", None,
                          None))
        self.assertEqual(index["source"]["drive_path"], f"{ART_PATH}/00 Index.html")
        self.assertEqual((b1["revision"]["version_id"], b1["revision"]["etag"]),
                         ("1.0", f'"{unique("20")},1"'))
        # parents, member path, aliases (identical bytes: both identities, one payload)
        self.assertEqual((po["parent"], issues["parent"], member["parent"]),
                         (pack["id"], pack["id"], issues["id"]))
        self.assertEqual((member["member_path"], po["msg_storage"]),
                         ("issues/B1.md", "__attach_version1.0_#00000000/"))
        self.assertNotEqual(b1["id"], b3["id"])
        self.assertEqual((b1["alias_of"], b3["alias_of"], member["alias_of"]),
                         (None, b1["id"], b1["id"]))
        self.assertEqual(member["same_bytes_as"], sorted([b1["id"], b3["id"]]))
        baseline_po = self.rows[8]  # the baseline's EML attachment with the same bytes
        self.assertEqual((po["alias_of"], po["same_bytes_as"]),
                         (baseline_po["id"], [baseline_po["id"]]))
        self.assertNotIn(po["id"], baseline_po.get("same_bytes_as") or [])
        self.assertEqual({tuple(r["case_ids"]) for r in added}, {("case-art",)})
        self.assertEqual({r["case"] for r in added}, {"case-art"})
        self.assertEqual({tuple(r["proven_by"]) for r in added}, {()})  # the read
        # the header: the record's commit, SHA-256, sections, payloads and caveats verbatim
        head = registry["advisories"][-1]
        self.assertEqual((head["role"], head["commit"], head["path"], head["record_sha256"],
                          head["source_revision"], head["case"]),
                         ("manifest", "e" * 40, "context/m.json", h(raw),
                          "sha256:" + "7" * 64, "case-art"))
        self.assertEqual(head["sections"], {"source_manifest": 4,
                                            "nested_attachment_manifest": 2,
                                            "zip_member_manifest": 1})
        self.assertEqual(head["payloads"], {"source_manifest": 3,
                                            "nested_attachment_manifest": 2,
                                            "zip_member_manifest": 1, "all": 5})
        self.assertEqual(head["caveats"], {"directive": "642494a1e893", "items": [
            {"id": k, "text": v} for k, v in CAVEATS.items()]})
        counts = registry["counts"]
        self.assertEqual(counts["manifest_rows"], 7)
        self.assertEqual(counts["payloads"], len({r["expected"]["sha256"] for r in rows
                                                  if r["status"] == "pinned"}))
        self.assertEqual(counts["payloads"] + 4, counts["pinned"])  # 4 rows repeat bytes
        self.assertEqual(counts["aliases"], 1 + 3)
        # the watch folder the files live in derives them
        art = next(w for w in registry["watch_folders"] if w["drive_id"] == MDRIVE)
        self.assertEqual(art["derived_from"], sorted(r["id"] for r in (b1, b3, pack, index)))
        # no URL, record text or body in the registry; the registry loads
        text = json.dumps(added) + json.dumps(head)
        for leak in ("https://", MAP_LINK, "7K2", "3J8", "2W6"):
            self.assertNotIn(leak, text)
        self.path.write_text(json.dumps(registry), encoding="utf-8")
        self.assertEqual(len(co.load_registry(self.path)[0]["rows"]), len(rows))
        # through the CLI: the command names the manifest and caveats, never the body
        files = {}
        for name, data in (("base.json", self.raw), ("delta.json", json.dumps(delta())),
                           ("m.json", raw), ("c.json", json.dumps(caveats(h(raw))))):
            files[name] = self.root / name
            files[name].write_bytes(data if isinstance(data, bytes) else data.encode())
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            code = co.main(["registry", "--from", str(files["base.json"]),
                            "--advisory-commit", COMMIT, "--advisory-path", "context/x.json",
                            "--delta", str(files["delta.json"]), "--delta-commit", "d" * 40,
                            "--delta-path", "context/y.json", "--watch-link", PROCESS,
                            "--watch-drive", DRIVE, "/Examples/F", ART_URL,
                            "--watch-drive", MDRIVE, ART_PATH, ART_WEB,
                            "--manifest", "case-art", "e" * 40, "context/m.json",
                            str(files["m.json"]), "--caveats", str(files["c.json"]),
                            "--out", str(self.root / "cli.json")])
        self.assertEqual(code, 0)
        cli = json.loads((self.root / "cli.json").read_text(encoding="utf-8"))
        self.assertEqual(cli["rows"], registry["rows"])
        self.assertIn(" --manifest case-art " + "e" * 40 + " context/m.json <manifest.json>",
                      cli["generated_by"])
        self.assertIn(" --caveats ", cli["generated_by"])
        self.assertNotIn(str(self.root), cli["generated_by"])

    def test_an_unknown_or_malformed_manifest_section_fails_closed(self):
        good = manifest()

        def changed(edit):
            record = json.loads(json.dumps(good))
            edit(record)
            return record

        def entry(section, index, **change):
            def edit(record):
                record[section][index].update(change)
                for key, value in change.items():
                    if value is KeyError:
                        del record[section][index][key]
            return edit

        refused = {
            "manifest_record_invalid": [changed(lambda r: r.update(schema="plx-advisory/v2")),
                                        [good], "not json"],
            "manifest_section_unknown": [changed(lambda r: r.update(
                attachment_text_manifest=[])), changed(lambda r: r.update(zip_members=[]))],
            "manifest_section_invalid": [changed(lambda r: r.update(zip_member_manifest={})),
                                         changed(lambda r: r.update(source_manifest=[]))],
            "manifest_entry_invalid:source_manifest:0": [
                changed(entry("source_manifest", 0, sha256="not-hex")),
                changed(entry("source_manifest", 0, size_bytes="13")),
                changed(entry("source_manifest", 0, name="a/b.md")),
                changed(entry("source_manifest", 0, list_id="not-a-guid")),
                changed(entry("source_manifest", 0, site_id=f"{SP},{MSITE}")),
                changed(entry("source_manifest", 0, canonical_url="http://x/y")),
                changed(entry("source_manifest", 0, list_item_unique_id=KeyError)),
                changed(entry("source_manifest", 0, owner="someone")),
                changed(entry("source_manifest", 0, revision={"list_etag": '"other,1"'}))],
            "manifest_entry_invalid:nested_attachment_manifest:1": [
                changed(entry("nested_attachment_manifest", 1, bytes=-1)),
                changed(entry("nested_attachment_manifest", 1, storage_prefix=""))],
            "manifest_entry_invalid:zip_member_manifest:0": [
                changed(entry("zip_member_manifest", 0, matching_top_level_list_item_ids="22")),
                changed(entry("zip_member_manifest", 0, name="../x.md"))],
            "manifest_entry_duplicate:source_manifest:1": [changed(entry(
                "source_manifest", 1, list_item_unique_id=unique("20"),
                revision=good["source_manifest"][0]["revision"]))],
            "manifest_parent_unresolved:nested_attachment_manifest:0": [
                changed(entry("nested_attachment_manifest", 0, parent_sha256="0" * 64)),
                changed(entry("nested_attachment_manifest", 0, parent_list_item_id="99"))],
            "manifest_parent_unresolved:zip_member_manifest:0": [
                changed(lambda r: (r["nested_attachment_manifest"].pop(),
                                   r["scope"].update(nested_attachments_hashed=1))),
                changed(lambda r: (r["nested_attachment_manifest"].append(
                    {**r["nested_attachment_manifest"][1], "name": "second.zip",
                     "sha256": "9" * 64, "storage_prefix": "p2/"}),
                    r["scope"].update(nested_attachments_hashed=3)))],
            "manifest_match_unresolved:zip_member_manifest:0": [
                changed(entry("zip_member_manifest", 0,
                              matching_top_level_list_item_ids=["22", "13"])),
                changed(entry("zip_member_manifest", 0,
                              matching_top_level_list_item_ids=["77"]))],
            "manifest_entry_unrouted:source_manifest:0": [
                changed(entry("source_manifest", 0, canonical_url=f"https://{SP}/sites/PD/"
                                                                  "Elsewhere/B1%20approve.md")),
                changed(entry("source_manifest", 0, list_id=str(uuid.UUID(int=9))))],
            "manifest_count_mismatch:top_level_originals": [changed(lambda r: r["scope"].update(
                top_level_originals=5))],
            "manifest_count_mismatch:unique_top_level_payloads": [changed(
                lambda r: r["scope"].update(unique_top_level_payloads=4))],
        }
        for reason, records in refused.items():
            for record in records:
                raw = record.encode() if isinstance(record, str) else json.dumps(record).encode()
                with self.subTest(reason=reason), self.assertRaisesRegex(ValueError, reason):
                    self.with_manifest(raw, notes=False)
        for kwargs, reason in (({"case": "case art"}, "manifest_case_or_commit_invalid"),
                               ({"commit": "abc"}, "manifest_case_or_commit_invalid")):
            with self.subTest(kwargs=kwargs), self.assertRaisesRegex(ValueError, reason):
                self.with_manifest(notes=False, **kwargs)
        # a manifest that repeats an existing row's identity is refused, not merged
        raw = json.dumps(good).encode()
        with self.assertRaisesRegex(ValueError, "registry_ids_not_unique|manifest_duplicate"):
            self.build(manifests=[("case-art", raw, "e" * 40, "context/m.json")] * 2,
                       watch_drives=[(MDRIVE, ART_PATH, ART_WEB)])
        # caveats: bound to a record given, well formed, never guessed
        ok = caveats(h(raw))
        for notes, reason in (
                (caveats("0" * 64), "caveats_advisory_unknown"),
                ({**ok, "schema": "x"}, "caveats_invalid"),
                ({**ok, "directive": "not a directive"}, "caveats_invalid"),
                ({**ok, "caveats": [{"id": "a", "text": ""}]}, "caveats_invalid"),
                ({**ok, "caveats": [{"id": "a", "text": "x"}] * 2}, "caveats_invalid"),
                ({**ok, "caveats": [{"id": "a", "text": "x", "note": "y"}]}, "caveats_invalid"),
                ({**ok, "extra": 1}, "caveats_invalid")):
            with self.subTest(reason=reason, notes=notes), \
                    self.assertRaisesRegex(ValueError, reason):
                self.with_manifest(raw, caveats=[json.dumps(notes).encode()])

    def test_manifest_rows_read_by_drive_path_and_their_members_through_the_container(self):
        registry = self.with_manifest()
        self.path.write_text(json.dumps(registry), encoding="utf-8")
        added = {r["name"]: r for r in registry["rows"] if r.get("origin") == "manifest"}
        report = self.run_read(self.manifest_tenant())
        entries = self.entries(report)
        self.assertEqual((report["unreported"], report["undiagnosed"]), (0, 0))
        self.assertEqual(report["read_via"]["drive_path"], 4)
        for name, via in (("B1 approve.md", "drive_path"), ("B3 lotion.md", "drive_path"),
                          ("issues pack.eml", "drive_path"), ("00 Index.html", "drive_path"),
                          ("PO#1.pdf", "container"), ("issues.zip", "container"),
                          ("issues/B1.md", "container")):
            with self.subTest(name=name):
                got = entries[added[name]["id"]]
                self.assertEqual((got["status"], got["source"], got["sha256_match"]),
                                 ("read", via, True))
        receipt = self.receipt(added["B1 approve.md"]["id"])
        self.assertEqual((receipt["canonical_source"]["drive_path"], receipt["read_via"],
                          receipt["proven_by"]),
                         (f"{ART_PATH}/B Gallons/01 Approve/B1 approve.md", "drive_path", []))
        # the located file is now a registered item: the watch folder does not rediscover it
        art = next(w for w in report["watch_folders"]
                   if w["id"] == next(f["id"] for f in registry["watch_folders"]
                                      if f["drive_id"] == MDRIVE))
        self.assertEqual((art["files"], art["registered"], art["discovered"]), (1, 1, 0))
        self.assertTrue(report["ok"])
        # a path that now holds another item, or none: gone and named, never read as it
        tenant = self.manifest_tenant()
        tenant.etags[(MDRIVE, "01MI22")] = '"{BBBBBBBB-0000-4000-8000-000000000022},1"'
        del tenant.files[(MDRIVE, "01MI41")]
        report = self.run_read(tenant, run_id="run-m2")
        entries = self.entries(report)
        replaced, missing = entries[added["B3 lotion.md"]["id"]], entries[
            added["00 Index.html"]["id"]]
        self.assertEqual((replaced["status"], replaced["detail"]),
                         ("gone", "path_holds_other_item"))
        self.assertEqual((missing["status"], missing["detail"]), ("gone", "path_not_located"))
        self.assertFalse([u for _, u in tenant.calls if "i=01MI22&" in u])
        # a listing the reader's permission filter thinned: not located is not gone
        from integrations.graph_read import SharePointReader
        listed = SharePointReader.list_folder

        def withholding(reader, *args, **kwargs):
            got = listed(reader, *args, **kwargs)
            kept = [i for i in got["items"] if i.get("name") != "00 Index.html"]
            done = {**got["completeness"], "withheld_count":
                    got["completeness"].get("withheld_count", 0) + len(got["items"]) - len(kept)}
            return {**got, "items": kept, "completeness": done}

        with mock.patch.object(SharePointReader, "list_folder", withholding):
            entries = self.entries(self.run_read(self.manifest_tenant(), run_id="run-m4"))
        hidden = entries[added["00 Index.html"]["id"]]
        self.assertEqual((hidden["status"], hidden["detail"]), ("unavailable", "path_withheld"))
        # a case-only rename: SharePoint names are case-insensitive, the eTag binds identity
        tenant = self.manifest_tenant()
        key = (MDRIVE, "01MI20")
        tenant.files[key] = (*tenant.files[key][:2], "b1 APPROVE.md", tenant.files[key][3])
        renamed = self.entries(self.run_read(tenant, run_id="run-m5"))[added["B1 approve.md"]["id"]]
        self.assertEqual((renamed["status"], renamed["source"]), ("read", "drive_path"))
        # the drive path's folder unreadable: the file and its members say why
        tenant = self.manifest_tenant()
        tenant.denied.add((MDRIVE, "01MESC"))
        entries = self.entries(self.run_read(tenant, run_id="run-m3"))
        for name in ("issues pack.eml", "PO#1.pdf", "issues.zip", "issues/B1.md"):
            self.assertEqual(entries[added[name]["id"]]["status"], "denied")

    def test_manifest_rows_one_folder_listing_and_one_read_each_at_any_count(self):
        """N = 1, 10, 60 files in one folder: each read exactly once, the folder listed
        once, no entry lost or duplicated, a constant number of requests per file."""
        sent = {}
        for n in (1, 10, 60):
            registry = self.with_manifest(manifest(extra=n))
            self.path.write_text(json.dumps(registry), encoding="utf-8")
            tenant = self.manifest_tenant(extra=n)
            report = self.run_read(tenant, run_id=f"run-n{n}")
            entries = self.entries(report)
            bulk_ids = [r["id"] for r in registry["rows"] if r.get("origin") == "manifest"
                        and r["name"].startswith("bulk-")]
            self.assertEqual(len(bulk_ids), n)
            self.assertEqual({entries[i]["status"] for i in bulk_ids}, {"read"})
            self.assertEqual(report["read_via"]["drive_path"], 4 + n)
            listings = [u for _, u in tenant.calls
                        if unquote(urlsplit(u).path).endswith("/B Gallons/05 Bulk:")]
            self.assertEqual(len(listings), 1)
            downloads = [u for _, u in tenant.calls if "download.aspx" in u]
            for item, *_ in bulk(n):
                self.assertEqual(sum(f"i=01MI{item}&" in u for u in downloads), 1)
            self.assertEqual(report["reported"], report["entries"])
            self.assertTrue(report["ok"])
            sent[n] = report["requests_sent"]
        self.assertEqual((sent[60] - sent[10]) * 9, (sent[10] - sent[1]) * 50)

    def test_crosswalk_maps_each_watched_source_and_descriptor_or_says_why(self):
        base = self.build(watch_links=[PROCESS], watch_drives=[(DRIVE, "/Examples/F",
                                                                ART_URL)])
        folder = base["watch_folders"][1]["id"]
        rows = {r["id"]: r for r in base["rows"]}
        qrm = next(r for r in base["rows"] if r["source"].get("item_id") == "01QRM")
        note = next(r for r in base["rows"] if r.get("member_path") == "Ex/A/note.txt")
        gap = next(r["id"] for r in base["rows"] if r["status"] == "gap")
        watched = {"note": "pins", "folders": {"G": folder}, "files": [
            {"case": "G", "name": "risk.pdf", "item_id": "01QRM", "sha256": h(QRM),
             "bytes": len(QRM), "advisory": "advisory:aaaaaaaaaaa1"},
            {"case": "G", "name": "thread.txt", "item_id": "01TXT", "sha256": "1" * 64,
             "bytes": 3, "advisory": "advisory:aaaaaaaaaaa1"},
            {"case": "G", "name": "copy.txt", "item_id": "01COPY", "sha256": h(NOTE),
             "bytes": len(NOTE), "advisory": "advisory:aaaaaaaaaaa2"},
            {"case": "G", "name": "Q1.xlsm", "item_id": "01Q1", "sha256": "2" * 64,
             "bytes": 9, "advisory": "advisory:aaaaaaaaaaa2"}]}
        quote_input = proposals("quote-x", {
            "advisory": {"id": "advisory:aaaaaaaaaaa0", "path": "context/q.json"},
            "related_advisories": [{"id": "advisory:aaaaaaaaaaa9"}],
            "originals_registry": "examples/case_originals.json",
            "watched_originals": watched})
        zip_input = proposals("screen-x", {
            "advisory": {"id": "advisory:bbbbbbbbbbb0"},
            "original_zip": {"name": "Map.zip", "location": "AGENTS/Use Cases",
                             "drive_id": DRIVE, "item_id": "01SZ", "bytes": 5,
                             "sha256": "3" * 64, "members": {"Map/a.html": "4" * 64},
                             "read_status": "members not in this repository"},
            "originals_registry": {"path": "examples/case_originals.json",
                                   "rows": {gap: "offered, not submitted"}}})
        inputs = [("evals/dev/q/proposed_items.json", quote_input),
                  ("evals/dev/s/proposed_items.json", zip_input)]
        registry = self.build(watch_links=[PROCESS], crosswalks=inputs,
                              watch_drives=[(DRIVE, "/Examples/F", ART_URL)])
        self.assertEqual(registry["rows"], base["rows"])  # the crosswalk adds no row
        walk = registry["crosswalk"]
        self.assertEqual([(i["path"], i["sha256"], i["case"]) for i in walk["inputs"]],
                         [(p, h(raw), c) for (p, raw), c in zip(inputs,
                                                                ("quote-x", "screen-x"))])
        got = [(r["case"], r["kind"], r["record"], r["item_id"], r["registry_row"],
                r["unmapped_reason"], r["watch_folder"]) for r in walk["rows"]]
        self.assertEqual(got, [
            ("quote-x", "watched_source", "advisory:aaaaaaaaaaa1", "01QRM", qrm["id"], None,
             folder),
            ("quote-x", "watched_source", "advisory:aaaaaaaaaaa1", "01TXT", None,
             "registry_row_bytes_differ", folder),
            ("quote-x", "watched_source", "advisory:aaaaaaaaaaa2", "01COPY", None,
             "bytes_match_other_identity", folder),
            ("quote-x", "watched_source", "advisory:aaaaaaaaaaa2", "01Q1", None,
             "watched_not_pinned", folder),
            ("screen-x", "descriptor", "advisory:bbbbbbbbbbb0", "01SZ", None,
             "descriptor_not_pinned", None),
            ("screen-x", "registry_reference", "advisory:bbbbbbbbbbb0", None, gap, None,
             None)])
        self.assertEqual([r["candidates"] for r in walk["rows"]],
                         [[], [self.rows[1]["id"]], [note["id"]], [], [], []])
        self.assertEqual(walk["rows"][0]["match_basis"], "item_id+sha256")
        self.assertEqual(walk["counts"], {"rows": 6, "mapped": 2, "unmapped": 4,
                                          "by_kind": {"watched_source": 4, "descriptor": 1,
                                                      "registry_reference": 1},
                                          "by_reason": {"registry_row_bytes_differ": 1,
                                                        "bytes_match_other_identity": 1,
                                                        "watched_not_pinned": 1,
                                                        "descriptor_not_pinned": 1}})
        self.assertEqual(len({r["id"] for r in walk["rows"]}), 6)
        self.assertIn(gap, rows)
        text = json.dumps(walk)
        for leak in ("https://", "AGENTS/Use Cases", "members not in", "Map/a.html", "4" * 64):
            self.assertNotIn(leak, text)

        def attempt(case, source, fmt="plx-eval-case-card-proposals/v1"):
            raw = json.dumps({"format": fmt, "case": case, "source": source}).encode()
            return self.build(crosswalks=[("evals/x.json", raw)], watch_links=[PROCESS],
                              watch_drives=[(DRIVE, "/Examples/F", ART_URL)])

        ok = json.loads(quote_input)["source"]
        bad_file = {**watched["files"][0]}
        refused = {
            "crosswalk_input_invalid": [("quote-x", ok, "plx-other/v1"), ("bad case", ok)],
            "crosswalk_input_empty": [("quote-x", {"advisory": ok["advisory"]})],
            "crosswalk_source_unknown": [("quote-x", {**ok, "mystery": {"x": 1}})],
            "crosswalk_watch_folder_unknown": [("quote-x", {**ok, "watched_originals": {
                **watched, "folders": {"G": "wf-000000000000"}}})],
            "crosswalk_entry_invalid": [
                ("quote-x", {**ok, "watched_originals": {**watched, "files": [
                    {**bad_file, **change}]}})
                for change in ({"sha256": "x"}, {"item_id": None}, {"advisory": "a:1"},
                               {"case": "H"}, {"bytes": "9"})],
            "crosswalk_registry_row_unknown": [("screen-x", {
                "advisory": {"id": "advisory:bbbbbbbbbbb0"},
                "originals_registry": {"path": "x", "rows": {"gap-000000000000": "x"}}})],
            "crosswalk_record_invalid": [("screen-x", {
                "advisory": {"id": "not-an-advisory"},
                "originals_registry": {"path": "x", "rows": {gap: "x"}}})]}
        for reason, cases in refused.items():
            for args in cases:
                with self.subTest(reason=reason, args=args), \
                        self.assertRaisesRegex(ValueError, reason):
                    attempt(*args)
        with self.assertRaisesRegex(ValueError, "crosswalk_input_invalid"):
            self.build(crosswalks=[("evals/x.json", b"not json")])

    def card_sources(self, data: bytes, **over) -> tuple[bytes, dict, str]:
        """A card-sources input naming one Teams chat attachment (and one gap), the
        source record that pins it, and the attachment's content URL."""
        url = personal("laura", "flow-mapper.zip")
        thread = "19%3A0c75e4d6abc%40thread.v2/1792424594223"
        record_doc = {"schema": "plx-advisory/v1", "kind": "context", "source_id": url,
                      "source_revision": "sha256:" + h(data),
                      "evidence": [{"source": url, "detail": "the attachment"},
                                   {"source": f"https://teams.microsoft.com/l/message/{thread}",
                                    "detail": "the chat message that shared it"}]}
        attachment = {"name": "flow-mapper.zip", "owner": "Laura", "url": url,
                      "teams_message_url": f"https://teams.microsoft.com/l/message/{thread}"
                                           "?context=%7b%22contextType%22%3a%22chat%22%7d",
                      "attachment_id": "3f6962f7-39ac-4675-b1fb-844893e7705d",
                      "submitted_at": "2026-10-19T15:43:14.223Z", "bytes": len(data),
                      "sha256": h(data), "advisory": {"commit": "e" * 40,
                                                      "path": "context/card.json"}}
        attachment.update(over)
        sources = {"schema": co.CARD_SOURCES_SCHEMA, "note": "test", "entries": [
            {"case": "case-brand", "chat_attachment": attachment},
            {"case": "case-rnd", "gap": {"item": "an artifact-only map", "owner": "Erin",
                                         "source": "an artifact invitation",
                                         "evidence": "advisory:aaaaaaaaaaaa",
                                         "status": "no bytes held or pinned"}}]}
        return json.dumps(sources).encode(), record_doc, url

    def test_a_card_sourced_teams_attachment_is_routed_read_and_gated_by_its_chat(self):
        from operating.captured_originals import source_unit

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("brand-map.md", "# Brand requirements map\n")
        data = buffer.getvalue()
        sources, record_doc, url = self.card_sources(data)
        records = {("e" * 40, "context/card.json"): json.dumps(record_doc).encode()}
        registry = self.build(card_sources=sources, card_records=records)
        [row] = [r for r in registry["rows"] if r["origin"] == "card" and "source" in r]
        [gap] = [r for r in registry["rows"] if r["origin"] == "card" and "gap" in r]
        self.assertEqual((row["status"], row["kind"], row["case_ids"], row["expected"]),
                         ("pinned", "original", ["case-brand"],
                          {"bytes": len(data), "sha256": h(data)}))
        self.assertEqual((row["source"]["url"], row["source"]["teams_message_id"],
                          row["source"]["teams_attachment_id"]),
                         (url, "1792424594223", "3f6962f7-39ac-4675-b1fb-844893e7705d"))
        self.assertEqual((gap["status"], gap["case"], gap["expected"]),
                         ("gap", "case-rnd", {"bytes": None, "sha256": None}))
        self.assertEqual(registry["advisories"][-1]["role"], "card")
        self.assertEqual(registry["advisories"][-1]["record_sha256"], h(records[(
            "e" * 40, "context/card.json")]))
        # read through the attachment's content URL (graph.share_drive_item), GET only
        tenant = Tenant()
        tenant.files[(PERSONAL, "01LAURA")] = ("/Microsoft Teams Chat Files", "01FT",
                                               "flow-mapper.zip", data)
        tenant.shares[encode_share_id(url)] = (PERSONAL, "01LAURA")
        self.path.write_text(json.dumps(registry), encoding="utf-8")
        report = self.run_read(tenant)
        self.assertEqual({m for m, _ in tenant.calls}, {"GET", "POST"})  # POST: token only
        receipt = self.receipt(row["id"])
        self.assertEqual((receipt["body_status"], receipt["match"], receipt["read_via"],
                          receipt["sha256"]), ("read", True, "shared_link", h(data)))
        self.assertGreaterEqual(report["read_via"]["shared_link"], 1)
        # served behind the chat message's gate, the OneDrive owner's exclusion first
        unit, reason = source_unit(receipt)
        self.assertEqual((unit["kind"], unit["chat_id"], unit["message_id"],
                          unit["onedrive_owner"], reason),
                         ("teams_chat", "19:0c75e4d6abc@thread.v2", "1792424594223",
                          "laura_x_com", None))
        # a Graph refusal of the link is that row's own typed finding
        tenant.shares.pop(encode_share_id(url))
        self.run_read(tenant, run_id="run-2")
        self.assertEqual(self.receipt(row["id"])["body_status"], "gone")

    def test_a_card_source_its_record_does_not_prove_fails_closed(self):
        record_key = ("e" * 40, "context/card.json")

        def attempt(sources, record_doc, records=None):
            co.build_registry(
                self.raw, advisory_commit=COMMIT, advisory_path="context/x.json",
                card_sources=sources, card_records=records if records is not None else {
                    record_key: json.dumps(record_doc).encode()})

        data = b"PK\x05\x06" + b"\0" * 18   # an empty zip archive no other row pins
        sources, good, url = self.card_sources(data)
        refused = {
            # the record names other bytes, another file or another message
            "card_attachment_unproven": [
                (sources, {**good, "source_revision": "sha256:" + "0" * 64}),
                (sources, {**good, "source_id": personal("laura", "other.zip")}),
                (sources, {**good, "evidence": good["evidence"][:1]}),
                (self.card_sources(data, url=personal("laura", "other.zip"))[0], good),
                (self.card_sources(data, sha256="0" * 64)[0], good)],
            # a channel message link, a non-chat link, an unknown key, a malformed pin
            "card_attachment_invalid": [
                (self.card_sources(data, teams_message_url="https://teams.microsoft.com/l/"
                                   "message/19%3Ax%40thread.tacv2/1?context=%7b%22contextType"
                                   "%22%3a%22channel%22%7d")[0], good),
                (self.card_sources(data, teams_message_url=url)[0], good),
                (self.card_sources(data, bytes=-1)[0], good),
                (self.card_sources(data, advisory={"commit": "e" * 40,
                                                  "path": "../card.json"})[0], good)],
            "card_source_invalid": [(self.card_sources(data, extra="x")[0], good)],
            "card_sources_invalid": [(b"[]", good), (json.dumps(
                {"schema": "plx-case-originals-card-sources/v0", "entries": []}).encode(),
                good)]}
        for reason, cases in refused.items():
            for sources_bytes, record_doc in cases:
                with self.subTest(reason=reason), self.assertRaisesRegex(ValueError, reason):
                    attempt(sources_bytes, record_doc)
        with self.assertRaisesRegex(ValueError, "card_advisory_missing"):
            attempt(sources, good, records={})
        # bytes another row already pins: the card names that row instead
        dup_sources, dup_record, _ = self.card_sources(MAP)
        with self.assertRaisesRegex(ValueError, "card_attachment_duplicates_row"):
            co.build_registry(self.raw, advisory_commit=COMMIT, advisory_path="context/x.json",
                              card_sources=dup_sources,
                              card_records={record_key: json.dumps(dup_record).encode()})

    def rendered_record(self, text: str, **over) -> dict:
        """An source record holding a claude.ai artifact's complete rendered text."""
        doc = {"schema": "plx-advisory/v1", "kind": "context",
               "source_id": "https://claude.ai/artifact/AbC123",
               "source_revision": "rendered-text-sha256:" + h(text.encode()),
               "summary": "R&D map: complete captured process map.",
               "facts": [{"type": "captured_source", "rendered_characters": len(text)},
                         {"type": "limits", "detail": "rendered text, not artifact bytes"}],
               "source_text": text}
        doc.update(over)
        return doc

    def rendered_sources(self, text: str, commit: str, **over) -> bytes:
        entry = {"name": "R&D map, complete rendered text.txt", "owner": "Erin",
                 "repository": "org/repo", "bytes": len(text.encode()),
                 "sha256": h(text.encode()),
                 "advisory": {"commit": commit, "path": "context/r.json"}}
        entry.update(over)
        return json.dumps({"schema": co.CARD_SOURCES_SCHEMA, "note": "test", "entries": [
            {"case": "case-rnd", "rendered_artifact": entry}]}).encode()

    def test_a_card_rendered_artifact_is_a_pinned_derivative_read_from_its_record(self):
        from operating.captured_originals import repository_resource, source_unit

        text = "Raw material samples \u2014 from request to used up\nStep 1 RS7Q.\n"
        repo = self.root / "repo"
        (repo / "context").mkdir(parents=True)
        (repo / "context" / "r.json").write_text(json.dumps(self.rendered_record(text)))
        git(repo, "init", "-q")
        git(repo, "add", ".")
        git(repo, "commit", "-qm", "record")
        commit = git(repo, "rev-parse", "HEAD")
        sources = self.rendered_sources(text, commit)
        with mock.patch.object(co, "REPOSITORY_ROOT", repo):
            records = co._card_records(sources)   # the local Git objects, no fetch
        registry = self.build(card_sources=sources, card_records=records)
        [row] = [r for r in registry["rows"] if r["origin"] == "card"]
        data = text.encode("utf-8")
        self.assertEqual((row["status"], row["kind"], row["record_kind"], row["case_ids"],
                          row["expected"], row["source"]["url"],
                          row["source"]["repository_ref"]),
                         ("pinned", "derivative", "historical complete rendered artifact "
                          "text, derived representation", ["case-rnd"],
                          {"bytes": len(data), "sha256": h(data)}, None,
                          f"https://github.com/org/repo/blob/{commit}/context/r.json"))
        self.assertEqual((registry["advisories"][-1]["role"],
                          registry["advisories"][-1]["source_id"]),
                         ("card", "https://claude.ai/artifact/AbC123"))
        self.assertNotIn("RS7Q", json.dumps(registry))   # no body in the registry
        # read from the record at its commit (the rendered-text field), no Graph request
        self.path.write_text(json.dumps(registry), encoding="utf-8")
        tenant = Tenant()
        with mock.patch.object(co, "REPOSITORY_ROOT", repo):
            report = self.run_read(tenant, watch=False)
        receipt = self.receipt(row["id"])
        self.assertEqual((receipt["body_status"], receipt["match"], receipt["read_via"],
                          receipt["sha256"], receipt["bytes"]),
                         ("read", True, "repository_field", h(data), len(data)))
        self.assertEqual(self.entries(report)[row["id"]]["status"], "read")
        self.assertEqual(report["read_via"]["repository"], 1)
        self.assertIn("RS7Q", (self.root / "state" / receipt["extraction"]["text_ref"])
                      .read_text(encoding="utf-8"))
        # served only to a reader granted the repository
        unit, reason = source_unit(receipt)
        self.assertEqual((unit["kind"], repository_resource(unit), reason),
                         ("repository", "repo:org/repo", None))
        # the record absent from the local objects: unavailable, never read
        moved = json.loads(json.dumps(registry))
        next(r for r in moved["rows"] if r["id"] == row["id"])["source"]["repository_ref"] = (
            f"https://github.com/org/repo/blob/{'0' * 40}/context/r.json")
        self.path.write_text(json.dumps(moved), encoding="utf-8")
        with mock.patch.object(co, "REPOSITORY_ROOT", repo):
            entry = self.entries(self.run_read(tenant, run_id="run-2", watch=False))[row["id"]]
        self.assertEqual((entry["status"], entry["detail"]),
                         ("unavailable", "repository_object_absent"))

    def test_a_rendered_artifact_its_record_does_not_prove_fails_closed(self):
        text = "Ingredient lists map\nStep 1.\n"
        commit, key = "e" * 40, ("e" * 40, "context/r.json")
        good = self.rendered_record(text)

        def attempt(sources, doc, records=None):
            co.build_registry(
                self.raw, advisory_commit=COMMIT, advisory_path="context/x.json",
                card_sources=sources, card_records=records if records is not None else {
                    key: json.dumps(doc).encode()})

        sources = self.rendered_sources(text, commit)
        attempt(sources, good)   # the proven record builds
        short = text[:-3]
        refused = {
            "card_rendered_unproven": [
                # another hash scheme, another text, a partial capture, no capture fact
                (sources, {**good, "source_revision": "sha256:" + h(text.encode())}),
                (sources, {**good, "source_text": short}),
                (sources, {**good, "facts": [{"type": "captured_source",
                                              "rendered_characters": len(text) + 40}]}),
                (sources, {**good, "facts": []}),
                (sources, {**good, "source_text": None}),
                (sources, {**good, "source_id": "artifact-invitation-email"}),
                (sources, {**good, "schema": "plx-advisory/v0"}),
                # the entry claims other bytes or another size
                (self.rendered_sources(text, commit, sha256=h(short.encode())), good),
                (self.rendered_sources(text, commit, bytes=len(text) + 1), good)],
            "card_rendered_invalid": [
                (self.rendered_sources(text, commit, repository="org"), good),
                (self.rendered_sources(text, commit, repository="../x/y"), good),
                (self.rendered_sources(text, commit, name="a/b.txt"), good),
                (self.rendered_sources(text, commit, advisory={"commit": commit,
                                                               "path": "../r.json"}), good),
                (self.rendered_sources(text, commit, bytes=-1), good)],
            "card_source_invalid": [(self.rendered_sources(text, commit, extra="x"), good)]}
        for reason, cases in refused.items():
            for sources_bytes, doc in cases:
                with self.subTest(reason=reason, doc=sorted(doc)), \
                        self.assertRaisesRegex(ValueError, reason):
                    attempt(sources_bytes, doc)
        with self.assertRaisesRegex(ValueError, "card_advisory_missing"):
            attempt(sources, good, records={})
        with self.assertRaisesRegex(ValueError, "card_advisory_invalid"):
            attempt(sources, good, records={key: b"not json"})

    def test_committed_card_rows_are_the_generators(self):
        # examples/case_originals_card_sources.json through the generator gives the
        # committed rows of origin card, the attachment proven by its source record
        # (read from the local Git objects; absent there, nothing can be checked).
        raw = (co.ROOT / "examples" / "case_originals_card_sources.json").read_bytes()
        records = co._card_records(raw)
        if not records:
            self.skipTest("the cited advisory record is not in this checkout's Git objects")
        heads, files, gaps = co._card_rows(raw, records)
        registry, _ = co.load_registry(co.REGISTRY_PATH)
        self.assertEqual([r for r in registry["rows"] if r.get("origin") == "card"],
                         files + gaps)
        self.assertEqual(registry["advisories"][-len(heads):], heads)
        zip_row, *maps = files
        self.assertEqual((zip_row["name"], zip_row["expected"]["sha256"][:8],
                          zip_row["source"]["teams_message_id"]),
                         ("regulatory-process-flow-mapper.zip", "1c081e63", "1792424594223"))
        # the three R&D maps: derivatives pinned by their records' rendered-text SHA-256,
        # read from the source record at its codex/record commit; no R&D gap row left
        self.assertEqual(gaps, [])
        self.assertEqual(
            [(r["id"], r["owner"], r["kind"], r["expected"]["sha256"][:8],
              r["source"]["repository_ref"].split("/blob/")[1][:8]) for r in maps],
            [("co-8302d5d6b9e4", "Allison Tam", "derivative", "a2d09819", "57bce1d4"),
             ("co-ef51ec46eba3", "Erin", "derivative", "60cfb8f7", "81c7833e"),
             ("co-6783155370e7", "Giulia", "derivative", "28145aac", "d7af7e54")])
        for row, head in zip(maps, heads[1:]):
            self.assertEqual(head["source_revision"],
                             "rendered-text-sha256:" + row["expected"]["sha256"])
        self.assertEqual(registry["counts"], co._counts(registry["rows"]))

    def test_committed_registry_carries_the_artwork_manifest_and_the_crosswalk(self):
        registry, _ = co.load_registry(co.REGISTRY_PATH)
        [head] = [a for a in registry["advisories"] if a["role"] == "manifest"]
        self.assertEqual((head["role"], head["commit"], head["path"], head["record_sha256"],
                          head["case"]),
                         ("manifest", "78640266849886c3816e9b78adbfcc6f64042aad",
                          "context/1ce04e07faab805fb8086d93864f6bad2c843649283fc1ba990f5d13b4b4"
                          "81ce.json",
                          "f2cd062d2ad182dbc8efc6024d89e0aa8b11702275a9b0b22b45e81693181276",
                          "vince-artwork"))
        self.assertEqual(head["sections"], {"source_manifest": 15,
                                            "nested_attachment_manifest": 18,
                                            "zip_member_manifest": 8})
        self.assertEqual(head["payloads"]["source_manifest"], 14)  # B1 and B3: one payload
        self.assertEqual(head["caveats"], {"directive": "642494a1e893", "items": [
            {"id": k, "text": v} for k, v in CAVEATS.items()]})
        added = [r for r in registry["rows"] if r.get("origin") == "manifest"]
        self.assertEqual(len(added), 41)
        self.assertEqual({tuple(r["proven_by"]) for r in added}, {()})
        self.assertEqual({r["case"] for r in added}, {"vince-artwork"})
        self.assertFalse([r["id"] for r in added if r["source"].get("url")
                          or "https://" in json.dumps(r)])
        b1, b3 = (next(r for r in added if r["source"].get("list_item_id") == i)
                  for i in ("20", "22"))
        self.assertEqual(b3["alias_of"], b1["id"])
        self.assertEqual(json.dumps(head).count("https://"), 0)
        # the crosswalk: the 28 watched quote sources and the Screening descriptor
        walk = registry["crosswalk"]
        kinds = [(r["case"], r["kind"]) for r in walk["rows"]]
        self.assertEqual(kinds.count(("quote-to-order", "watched_source")), 28)
        self.assertEqual(kinds.count(("vince-screening", "descriptor")), 1)
        for row in walk["rows"]:
            with self.subTest(row=row["id"]):
                self.assertTrue(row["record"].startswith("advisory:"))
                self.assertTrue(bool(row["registry_row"]) != bool(row["unmapped_reason"]))
        self.assertNotIn("https://", json.dumps(walk))

if __name__ == "__main__":
    unittest.main()
