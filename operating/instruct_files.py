"""Documents an instruction drafts (a letter, a summary, a spec, a report, a filled table)
made into real files, and their upload to SharePoint as a draft a person approves.

* **Files** (``make``): ``docx`` (WordprocessingML) and ``xlsx`` (SpreadsheetML) are
  written with the standard library alone; ``pdf`` needs the optional ``reportlab``
  (absent: refused, the caller offers a .docx). A file's bytes are the same for the same
  content (fixed zip timestamps), so its digest names it.
* **Uploads** (``UploadDesk``): an upload is prepared for one reader, one file and one
  target (a SharePoint drive and folder the reader opened through a file there: the
  ``anchor``, re-opened as the reader at preview and at approval, so a target they lost
  access to is refused). The reader must also hold a write or owner role on the folder
  itself (``folder_writable``: the SharePoint grant roles the capture recorded with the
  folder, or the grants the anchor file inherits from it), checked at preview and again
  at approval; a read-only reader, or one whose write access is not known, is refused:
  the app's own write grant never stands in for theirs. The preview shows before (the folder as cited, the file absent
  from this draft) and after (the folder with the new file: name, size, digest), and a
  ``preview_hash``; approval needs the reader's own confirm for that hash. The approved
  upload then runs through the gated write path (``autopilot_write.Executor``, SharePoint
  ``file_upload``: never a replace) as the reader's approval: it is made only when the
  owner opened SharePoint to approved writes and the preview the reader confirmed said so;
  otherwise it is journalled ``would_upload`` with what a live upload lacks.

Kept under ``<state>/instruct`` (0600 files in a 0700 folder; memory without a state
directory). Evidence label of tests: ``isolated-fixture``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import secrets
import threading
from typing import Any, Callable, Mapping, Optional
from xml.sax.saxutils import escape
import zipfile

KINDS = ("docx", "xlsx", "pdf")
MEDIA = {"docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
         "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
         "pdf": "application/pdf"}
MAX_CONTENT = 60_000
MAX_ROWS, MAX_COLS = 2000, 40
# SharePoint grant roles that let a person add a file to a folder
WRITE_ROLES = frozenset({"write", "owner"})
NO_WRITE = "Your SharePoint access to that folder does not include adding files."
WRITE_UNKNOWN = ("Whether you may add files to that folder is not known (its SharePoint "
                 "permissions were not read in full), so it is not uploaded.")
# the not-routine reasons an approval accepts, in the words the preview shows them
ACKNOWLEDGE_WORDS = {"operation_not_routine:file_upload":
                     "Adding a file to SharePoint is not a routine write: your confirm "
                     "approves this one upload."}
LIVE_MISSING = ("This build has no SharePoint upload path: the approved upload is recorded, "
                "not made.")
# the write path's outcome -> the upload's state (anything else: recorded, not made)
OUTCOME_STATUS = {"done": "uploaded", "failed": "failed", "unknown": "unknown",
                  "pending": "unknown"}
_NAME = re.compile(r"[^A-Za-z0-9 ._()-]+")
_FIXED = (2026, 1, 1, 0, 0, 0)


class FileRefused(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def file_name(raw: Any, kind: str) -> str:
    """A safe file name with the kind's extension."""
    base = _NAME.sub(" ", str(raw or "")).strip(" .")
    base = re.sub(r"\.(docx|xlsx|pdf)$", "", base, flags=re.I)[:80].strip() or "Document"
    return f"{base}.{kind}"


def _zip(parts: Mapping[str, str]) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for name, text in parts.items():
            info = zipfile.ZipInfo(name, _FIXED)
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, text.encode("utf-8"))
    return out.getvalue()


_RELS = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
         '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
         '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
         'relationships/officeDocument" Target="{target}"/></Relationships>')


def _docx(content: str) -> bytes:
    paras = []
    for line in content.split("\n"):
        run = f'<w:r><w:t xml:space="preserve">{escape(line)}</w:t></w:r>' if line else ""
        paras.append(f"<w:p>{run}</w:p>")
    body = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/'
            'main"><w:body>' + "".join(paras) + "</w:body></w:document>")
    types = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
             '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
             '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.'
             'relationships+xml"/><Default Extension="xml" ContentType="application/xml"/>'
             '<Override PartName="/word/document.xml" ContentType="application/vnd.'
             'openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
    return _zip({"[Content_Types].xml": types,
                 "_rels/.rels": _RELS.format(target="word/document.xml"),
                 "word/document.xml": body})


def _column(n: int) -> str:
    out = ""
    while True:
        n, r = divmod(n, 26)
        out = chr(65 + r) + out
        if n == 0:
            return out
        n -= 1


def _xlsx(content: str) -> bytes:
    rows = []
    for r, line in enumerate([ln for ln in content.split("\n") if ln.strip()][:MAX_ROWS], 1):
        cells = [c.strip() for c in line.split("|")][:MAX_COLS]
        xml = []
        for c, value in enumerate(cells):
            ref = f"{_column(c)}{r}"
            if re.fullmatch(r"-?\d+(?:\.\d+)?", value):
                xml.append(f'<c r="{ref}"><v>{value}</v></c>')
            else:
                xml.append(f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">'
                           f"{escape(value)}</t></is></c>")
        rows.append(f'<row r="{r}">' + "".join(xml) + "</row>")
    ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    sheet = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><worksheet {ns}>'
             "<sheetData>" + "".join(rows) + "</sheetData></worksheet>")
    book = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><workbook {ns} '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets></workbook>')
    book_rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships '
                 'xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                 '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/'
                 'officeDocument/2006/relationships/worksheet" Target="worksheets/'
                 'sheet1.xml"/></Relationships>')
    types = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
             '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
             '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.'
             'relationships+xml"/><Default Extension="xml" ContentType="application/xml"/>'
             '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.'
             'openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
             '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.'
             'openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>')
    return _zip({"[Content_Types].xml": types, "_rels/.rels": _RELS.format(
        target="xl/workbook.xml"), "xl/workbook.xml": book,
        "xl/_rels/workbook.xml.rels": book_rels, "xl/worksheets/sheet1.xml": sheet})


def _pdf(content: str) -> bytes:
    try:
        from reportlab.lib.pagesizes import letter
        from reportlab.pdfgen import canvas
    except ImportError:
        raise FileRefused("pdf_unavailable") from None
    out = io.BytesIO()
    pdf = canvas.Canvas(out, pagesize=letter, invariant=1)
    y = 750
    for line in content.split("\n"):
        for start in range(0, max(len(line), 1), 95):
            if y < 50:
                pdf.showPage()
                y = 750
            pdf.drawString(50, y, line[start:start + 95])
            y -= 14
    pdf.save()
    return out.getvalue()


def make(kind: str, content: str) -> bytes:
    """The file of ``kind`` holding ``content`` (lines; an xlsx row's cells split on "|")."""
    if kind not in KINDS:
        raise FileRefused("kind_unknown")
    if not content.strip() or len(content) > MAX_CONTENT:
        raise FileRefused("content_size")
    return {"docx": _docx, "xlsx": _xlsx, "pdf": _pdf}[kind](content)


# ---- the upload desk --------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class Target:
    """Where an upload goes: a SharePoint drive and folder, named by a file there the
    reader opened (``anchor``)."""
    anchor: str
    drive: str
    folder: str
    label: str
    parent: str = ""        # the folder's drive item id (the anchor file's parent), if known


def target_of(handle: str, fields: Mapping[str, str], label: str) -> Optional[Target]:
    """The folder of a ``sharepoint://<drive>/<item>@<eTag>`` file (its parent path when its
    fields state one); None for any other handle."""
    from integrations.live_source_guard import parse_handle
    try:
        (drive, _item), _v, _x = parse_handle(handle.split("#", 1)[0], "sharepoint", 2)
    except (ValueError, TypeError):
        return None
    folder = next((str(v) for k, v in fields.items()
                   if k.rsplit(".", 1)[-1].lower() in ("parent_path", "folder", "path")
                   and v), "")
    parent = next((str(v) for k, v in fields.items()
                   if k.rsplit(".", 1)[-1].lower() in ("parent_id", "parentid") and v), "")
    return Target(handle, drive, folder or "(the folder of " + label + ")", label, parent)


def _grants_held(acl: Any, identities: frozenset[str], *, inherited_only: bool
                 ) -> Optional[tuple[bool, int]]:
    """(whether a grant naming one of ``identities`` holds a write role, how many grants
    counted) from a recorded permission list (``graph_read.permission_acl``); None when
    it cannot say: not read in full, or a grant without its roles (or, for
    ``inherited_only``, without whether it is inherited)."""
    if isinstance(acl, str):
        try:
            acl = json.loads(acl)
        except ValueError:
            return None
    if not isinstance(acl, Mapping) or acl.get("status") != "read":
        return None
    grants = acl.get("grants")
    if not isinstance(grants, list):
        return None
    counted, held = 0, False
    for grant in grants:
        if not isinstance(grant, Mapping) or not isinstance(grant.get("roles"), list) \
                or not isinstance(grant.get("identities"), list):
            return None
        if inherited_only and not isinstance(grant.get("inherited"), bool):
            return None
        if inherited_only and not grant["inherited"]:
            continue        # the file's own grant: not the folder's
        counted += 1
        names = {str(i).strip().lower() for i in grant["identities"]}
        if names & identities and {str(r).lower() for r in grant["roles"]} & WRITE_ROLES:
            held = True
    return held, counted


def folder_writable(folder_acl: Any, anchor_acl: Any, identities: Any) -> Optional[bool]:
    """Whether a person holds a write or owner role on a SharePoint folder: from the
    folder's own recorded grants, else from the grants its file (the anchor) inherits from
    it. None: not known (no list read in full, a grant without its roles, a file with no
    inherited grants to read). Only grants naming the person's own identities count
    (their object id and addresses; a group or a sharing link never does)."""
    ids = frozenset(str(i).strip().lower() for i in identities or () if isinstance(i, str)
                    and i.strip() and not str(i).startswith(("group:", "link-scope:")))
    if not ids:
        return None
    folder = _grants_held(folder_acl, ids, inherited_only=False)
    if folder is not None:
        return folder[0]
    anchor = _grants_held(anchor_acl, ids, inherited_only=True)
    if anchor is None or (not anchor[0] and anchor[1] == 0):
        return None     # the file's permissions are its own: the folder's are not shown
    return anchor[0]


class UploadDesk:
    """Prepared and approved uploads and their files (module doc)."""

    def __init__(self, directory: Optional[Path]) -> None:
        self.dir = directory
        self._lock = threading.Lock()
        self._mem: list[dict[str, Any]] = []
        self._files: dict[str, bytes] = {}
        if directory is not None:
            (directory / "files").mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(directory, 0o700)

    @classmethod
    def for_state_dir(cls, state_dir: Any) -> "UploadDesk":
        return cls(Path(state_dir) / "instruct" if state_dir else None)

    def _rows(self) -> list[dict[str, Any]]:
        if self.dir is None:
            return list(self._mem)
        path = self.dir / "uploads.jsonl"
        if not path.is_file():
            return []
        return [json.loads(line) for line in path.read_text("utf-8").splitlines() if line]

    def _append(self, row: Mapping[str, Any]) -> None:
        if self.dir is None:
            self._mem.append(dict(row))
            return
        fd = os.open(self.dir / "uploads.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, sort_keys=True) + "\n")

    def keep_file(self, data: bytes) -> str:
        file_id = "f_" + hashlib.sha256(data).hexdigest()[:24]
        with self._lock:
            if self.dir is None:
                self._files[file_id] = data
            else:
                path = self.dir / "files" / file_id
                if not path.exists():
                    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(fd, "wb") as fh:
                        fh.write(data)
        return file_id

    def file(self, reader_key: str, file_id: str) -> Optional[bytes]:
        """A kept file, only for the reader an upload of it was prepared for."""
        if not re.fullmatch(r"f_[0-9a-f]{24}", file_id or ""):
            return None
        if not any(r["reader"] == reader_key and r.get("file_id") == file_id
                   for r in self._rows() if r["event"] == "prepared"):
            return None
        if self.dir is None:
            return self._files.get(file_id)
        path = self.dir / "files" / file_id
        return path.read_bytes() if path.is_file() else None

    def prepare(self, reader_key: str, *, name: str, kind: str, data: bytes,
                target: Optional[Target], people: tuple[str, ...] | list[str] = ()
                ) -> dict[str, Any]:
        """A draft upload (``people``: the addresses the document names, cleared by the
        people exclusion before it is made)."""
        digest = hashlib.sha256(data).hexdigest()
        where = target.__dict__ if target else None
        file_id = self.keep_file(data)
        with self._lock:
            # the same file to the same folder, still waiting for this reader: the same
            # draft (a retried instruction prepares no second upload)
            rows = self._rows()
            approved = {r.get("upload_id") for r in rows if r["event"] == "approved"}
            for r in reversed(rows):
                if r["event"] == "prepared" and r["reader"] == reader_key and \
                        r["digest"] == digest and r["name"] == name and r["kind"] == kind \
                        and r["target"] == where and r["upload_id"] not in approved:
                    return r
            row = {"event": "prepared", "upload_id": "up_" + secrets.token_hex(12),
                   "reader": reader_key, "file_id": file_id, "name": name, "kind": kind,
                   "size": len(data), "digest": digest, "target": where,
                   "people": sorted({str(p).lower() for p in people})[:200], "at": _now()}
            self._append(row)
        return row

    def get(self, reader_key: str, upload_id: str) -> Optional[dict[str, Any]]:
        """The reader's own upload with its state (``prepared`` or ``would_upload``)."""
        rows = [r for r in self._rows() if r.get("upload_id") == upload_id
                and r["reader"] == reader_key]
        first = next((r for r in rows if r["event"] == "prepared"), None)
        if first is None:
            return None
        done = next((r for r in rows if r["event"] == "approved"), None)
        outcome = next((r for r in rows if r["event"] == "outcome"), None)
        if done is None:
            status = "prepared"
        elif outcome is None:
            # approved, the write path not done with it yet: a live one may be made now
            status = "unknown" if done.get("live") else "would_upload"
        else:
            status = OUTCOME_STATUS.get(outcome.get("status"), "would_upload")
        return {**first, "status": status, "approved_at": done["at"] if done else None,
                "ran": outcome is not None,
                "reasons": list((outcome or {}).get("reasons") or ())}

    def record_outcome(self, reader_key: str, upload_id: str, status: str,
                       reasons: list[str]) -> None:
        """What the write path did with an approved upload (its own row; once)."""
        with self._lock:
            if not any(r["event"] == "outcome" and r.get("upload_id") == upload_id
                       for r in self._rows()):
                self._append({"event": "outcome", "upload_id": upload_id,
                              "reader": reader_key, "status": status,
                              "reasons": [str(x)[:200] for x in reasons][:8], "at": _now()})

    @staticmethod
    def preview(upload: Mapping[str, Any], opens: Callable[[str], bool],
                missing: Optional[list[str]] = None, *,
                writes: Callable[[Mapping[str, Any]], Optional[bool]],
                acknowledge: tuple[str, ...] = ()) -> dict[str, Any]:
        """Before and after for the upload as the reader may do it now (``opens``: whether
        the reader opens the target's anchor file now; ``writes``: whether they hold write
        access to the target folder now, None when not known: refused either way unless
        True; ``missing``: what a live upload lacks now, empty when approving it uploads,
        None when unknown; ``acknowledge``: the not-routine reasons the confirm accepts,
        shown and covered by the hash)."""
        target = upload.get("target") or None
        problems = []
        if target is None:
            problems.append("No SharePoint folder is chosen: open a file in the folder it "
                            "belongs in and ask again, or name the folder.")
        elif not opens(target["anchor"]):
            problems.append("You can no longer open the folder this goes to.")
        else:
            writable = writes(target)
            if writable is not True:
                problems.append(NO_WRITE if writable is False else WRITE_UNKNOWN)
        where = (target or {}).get("folder") or "no folder"
        after = f"{where} with {upload['name']} ({upload['size']} bytes) added"
        acknowledge = tuple(sorted(set(acknowledge)))
        shown = {"upload_id": upload["upload_id"], "name": upload["name"],
                 "digest": upload["digest"], "target": target,
                 "acknowledge": list(acknowledge)}
        return {"before": f"{where}, without {upload['name']}" if target else "Not chosen",
                "after": after if target else "Not chosen", "problems": problems,
                "missing": [LIVE_MISSING] if missing is None else list(missing),
                "live": missing == [],
                "acknowledge": acknowledge,
                "acknowledge_words": [ACKNOWLEDGE_WORDS.get(r, "Not a routine write: " + r)
                                      for r in acknowledge],
                # whether approving uploads is part of what the reader confirms
                "preview_hash": hashlib.sha256(json.dumps({**shown, "live": missing == []},
                                                          sort_keys=True)
                                               .encode()).hexdigest()}

    def approve(self, reader_key: str, upload: Mapping[str, Any], shown: Mapping[str, Any],
                *, confirm: bool, preview_hash: str) -> bool:
        """Record the reader's approval of the preview they saw; nothing is uploaded.
        True only for the one request that recorded it (a double click's second request,
        or a later one, gets False and must not run the upload)."""
        if confirm is not True:
            raise FileRefused("not_confirmed")
        if shown["problems"]:
            raise FileRefused("problems")
        if preview_hash != shown["preview_hash"]:
            raise FileRefused("preview_changed")
        with self._lock:
            if self.get(reader_key, upload["upload_id"])["status"] != "prepared":
                return False
            self._append({"event": "approved", "upload_id": upload["upload_id"],
                          "reader": reader_key, "preview_hash": preview_hash,
                          "live": bool(shown.get("live")), "at": _now()})
            return True


__all__ = ["FileRefused", "KINDS", "LIVE_MISSING", "MEDIA", "NO_WRITE", "Target",
           "UploadDesk", "WRITE_UNKNOWN", "file_name", "folder_writable", "make",
           "target_of"]
