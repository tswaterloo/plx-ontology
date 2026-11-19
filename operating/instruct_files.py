"""Documents an instruction drafts (a letter, a summary, a spec, a report, a filled table)
made into real files, and their upload to SharePoint as a draft a person approves.

* **Files** (``make``): ``docx`` (WordprocessingML), ``xlsx`` (SpreadsheetML) and ``pptx``
  (PresentationML: a slide per "#" heading) are written with the standard library alone;
  ``pdf`` needs the optional ``reportlab`` (absent: refused, the caller offers a .docx).
  Charts drawn from query rows (``operator_api.charts``) go where a "[chart N]" line
  stands, the rest at the end: native Office charts over their rows (``chart_ooxml``,
  ``chart_docs``), drawn in a PDF (``chart_pdf``). A file's bytes are the same for the same
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
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import secrets
import threading
from typing import Any, Callable, Mapping, Optional, Sequence
from xml.sax.saxutils import escape
import zipfile
from operating.card_text import mask_served
from operating.file_modes import keep_private

KINDS = ("docx", "xlsx", "pdf", "pptx", "csv")
MEDIA = {"docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
         "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
         "pdf": "application/pdf",
         "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
         "csv": "text/csv; charset=utf-8"}
MAX_DOC_CHARTS = 8
_CHART_AT = re.compile(r"^\s*\[chart\s+([1-9][0-9]?)\]\s*$", re.I)
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
    base = re.sub(r"\.(docx|xlsx|pdf|pptx|csv)$", "", base, flags=re.I)[:80].strip() or "Document"
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


# the Markdown a drafted document is written in, as Word shows it: "#" headings, "-"/"*"
# bullets, "**bold**", and a run of "|" lines a table (its "|---|" rule line dropped)
_HEADING = re.compile(r"^(#{1,3})\s+(.*)$")
_BULLET = re.compile(r"^\s*[-*]\s+(.*)$")
_RULE_ROW = re.compile(r"^\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
_HEADING_SIZE = {1: 32, 2: 28, 3: 24}   # half-points


def _runs(text: str, bold: bool = False, size: int = 0) -> str:
    """``text`` as Word runs, each ``**...**`` part bold."""
    out = []
    for n, part in enumerate(re.split(r"\*\*", text)):
        if not part:
            continue
        props = ("<w:b/>" if bold or n % 2 else "") + (f'<w:sz w:val="{size}"/>' if size else "")
        out.append(f"<w:r>{f'<w:rPr>{props}</w:rPr>' if props else ''}"
                   f'<w:t xml:space="preserve">{escape(part)}</w:t></w:r>')
    return "".join(out)


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _table(rows: list[list[str]]) -> str:
    width = max(len(r) for r in rows)
    edge = "".join(f'<w:{side} w:val="single" w:sz="4" w:space="0" w:color="999999"/>'
                   for side in ("top", "left", "bottom", "right", "insideH", "insideV"))
    body = "".join(
        "<w:tr>" + "".join(f"<w:tc><w:p>{_runs(c, bold=i == 0)}</w:p></w:tc>"
                           for c in r + [""] * (width - len(r))) + "</w:tr>"
        for i, r in enumerate(rows))
    return (f'<w:tbl><w:tblPr><w:tblW w:w="0" w:type="auto"/><w:tblBorders>{edge}'
            f"</w:tblBorders></w:tblPr>{body}</w:tbl>")


def _table_rows(lines: Sequence[str]) -> set[int]:
    """Which lines are rows of a Markdown table: a line starting with "|", or a run of
    lines with a "|" between cells whose header is followed by its rule row ("--- | ---")."""
    out = {i for i, line in enumerate(lines) if line.lstrip().startswith("|")}
    for i in range(len(lines) - 1):
        if "|" in lines[i] and _RULE_ROW.match(lines[i + 1].strip()) \
                and "|" in lines[i + 1]:
            j = i
            while j < len(lines) and "|" in lines[j] and lines[j].strip():
                out.add(j)
                j += 1
    return out


def _placed(content: str, charts: Sequence[Mapping[str, Any]]
            ) -> list[tuple[str, Optional[Mapping[str, Any]]]]:
    """The content's lines in order with each chart where its "[chart N]" line stands (a
    line naming no chart is dropped); the charts no line names follow at the end."""
    out: list[tuple[str, Optional[Mapping[str, Any]]]] = []
    used: set[int] = set()
    for line in content.split("\n"):
        at = _CHART_AT.match(line)
        if at:
            n = int(at.group(1)) - 1
            if n < len(charts) and n not in used:
                used.add(n)
                out.append(("", charts[n]))
            continue
        out.append((line, None))
    out += [("", c) for n, c in enumerate(charts) if n not in used]
    return out


def _docx(content: str, charts: Sequence[Mapping[str, Any]] = ()) -> bytes:
    from operating import chart_docs
    parts: list[str] = []
    table: list[list[str]] = []
    extra: dict[str, Any] = {}
    relations: list[tuple[str, str, str]] = []
    overrides: dict[str, str] = {}
    lines = _placed(content, charts) + [("", None)]
    rows = _table_rows([line for line, _ in lines])
    for at, (line, chart) in enumerate(lines):
        if chart is not None:
            if table:
                parts.append(_table(table))
                table = []
            body, chart_parts, chart_rels, chart_types = chart_docs.docx_chart(
                chart, len(relations) + 1)
            parts.append(body)
            extra.update(chart_parts)
            relations += chart_rels
            overrides.update(chart_types)
            continue
        if at in rows:
            if not _RULE_ROW.match(line.strip()):
                table.append(_cells(line))
            continue
        if table:
            parts.append(_table(table))
            table = []
        heading, bullet = _HEADING.match(line), _BULLET.match(line)
        if heading:
            size = _HEADING_SIZE[len(heading.group(1))]
            parts.append(f"<w:p>{_runs(heading.group(2), bold=True, size=size)}</w:p>")
        elif bullet:
            parts.append(f'<w:p><w:pPr><w:ind w:left="360" w:hanging="240"/></w:pPr>'
                         f"{_runs(chr(8226) + ' ' + bullet.group(1))}</w:p>")
        else:
            parts.append(f"<w:p>{_runs(line)}</w:p>")
    while parts and parts[-1] == "<w:p></w:p>":
        parts.pop()
    body = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<w:document {chart_docs.DOC_NS}><w:body>' + "".join(parts)
            + "</w:body></w:document>")
    if not relations:
        types = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                 '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                 '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.'
                 'relationships+xml"/><Default Extension="xml" ContentType="application/xml"/>'
                 '<Override PartName="/word/document.xml" ContentType="application/vnd.'
                 'openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
        return _zip({"[Content_Types].xml": types,
                     "_rels/.rels": _RELS.format(target="word/document.xml"),
                     "word/document.xml": body})
    from operating import chart_ooxml as ox
    types = ox.content_types({**ox.BASE_TYPES, "xlsx": ox.CT_XLSX}, {
        "/word/document.xml": "application/vnd.openxmlformats-officedocument."
        "wordprocessingml.document.main+xml", **overrides})
    return ox.zip_parts({"[Content_Types].xml": types,
                         "_rels/.rels": _RELS.format(target="word/document.xml"),
                         "word/document.xml": body,
                         "word/_rels/document.xml.rels": ox.rels(relations), **extra})


def _column(n: int) -> str:
    out = ""
    while True:
        n, r = divmod(n, 26)
        out = chr(65 + r) + out
        if n == 0:
            return out
        n -= 1


def _xlsx(content: str, charts: Sequence[Mapping[str, Any]] = ()) -> bytes:
    rows = []
    lines = [ln for ln in content.split("\n") if ln.strip() and not _CHART_AT.match(ln)]
    for r, line in enumerate(lines[:MAX_ROWS], 1):
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
    if charts:   # the text on the first sheet, each chart's rows and chart on its own
        from operating import chart_ooxml as ox
        sheets = [("Sheet1", {"xl/worksheets/sheet1.xml": sheet},
                   {"/xl/worksheets/sheet1.xml": ox.SHEET_CT})]
        first = 1
        for n, chart in enumerate(charts, 2):
            name = f"Chart {n - 1}"
            parts, types, made = ox.sheet_parts(chart, n, first, name)
            sheets.append((name, parts, types))
            first += made
        return ox.workbook(sheets)
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


def _pdf(content: str, charts: Sequence[Mapping[str, Any]] = ()) -> bytes:
    try:
        from reportlab.lib.pagesizes import letter
        from reportlab.pdfgen import canvas
    except ImportError:
        raise FileRefused("pdf_unavailable") from None
    from operating import chart_pdf
    out = io.BytesIO()
    pdf = canvas.Canvas(out, pagesize=letter, invariant=1)
    y = 750
    for line, chart in _placed(content, charts):
        if chart is not None:
            # the chart and the start of its notes on one page (long notes go on over)
            height = chart_pdf.HEIGHT + chart_pdf.NOTE_LEAD * min(
                len(chart_pdf.notes(chart, 512)), 6) + 50
            if y - height < 50:
                pdf.showPage()
                y = 750
            y = chart_pdf.draw(pdf, chart, 50, y, 512)
            continue
        heading = _HEADING.match(line)
        if heading:
            line = heading.group(2)
        pdf.setFont("Helvetica-Bold" if heading else "Helvetica",
                    {1: 15, 2: 13, 3: 12}[len(heading.group(1))] if heading else 10)
        pdf.setFillColor("#000000")
        for start in range(0, max(len(line), 1), 95):
            if y < 50:
                pdf.showPage()
                y = 750
            pdf.drawString(50, y, line[start:start + 95].replace("**", ""))
            y -= 18 if heading else 14
    pdf.save()
    return out.getvalue()


def _pptx(content: str, charts: Sequence[Mapping[str, Any]] = ()) -> bytes:
    """A deck: a slide per "#" heading (its lines below it; the first lines before any
    heading a slide of their own), and a slide per chart where it is placed."""
    from operating import chart_docs
    slides: list[dict[str, Any]] = []
    current: Optional[dict[str, Any]] = None
    for line, chart in _placed(content, charts):
        if chart is not None:
            if current is not None and current.get("chart") is None \
                    and len(current["lines"]) <= chart_docs.CHART_SLIDE_LINES:
                # placed under a heading with a few lines: the chart goes on that slide
                current["chart"] = chart
                current["title"] = current["title"] or str(chart.get("title") or "")
            else:
                current = {"title": str(chart.get("title") or ""), "lines": [],
                           "chart": chart}
                slides.append(current)
            continue   # the lines after it (until a heading) are that slide's too
        heading = _HEADING.match(line)
        if heading or current is None:
            if not heading and not line.strip():
                continue
            current = {"title": heading.group(2).replace("**", "") if heading else "",
                       "lines": []}
            slides.append(current)
            if heading:
                continue
        bullet = _BULLET.match(line)
        if line.strip() and not _RULE_ROW.match(line.strip()):
            current["lines"].append(("- " + bullet.group(1) if bullet else line.strip())
                                    .replace("**", ""))
    return chart_docs.pptx(slides)


def make(kind: str, content: str, charts: Sequence[Mapping[str, Any]] = ()) -> bytes:
    """The file of ``kind`` holding ``content`` (lines; a docx reads its Markdown headings,
    bullets, bold and tables; an xlsx row's cells split on "|"; a pptx a slide per
    heading) and ``charts`` (drawn charts, ``models.AskChart`` dicts, at most
    ``MAX_DOC_CHARTS``) where "[chart N]" lines place them."""
    if kind not in KINDS:
        raise FileRefused("kind_unknown")
    if len(content) > MAX_CONTENT or not (content.strip() or charts):
        raise FileRefused("content_size")
    if kind == "csv" and charts:
        raise FileRefused("charts_not_held")   # a CSV is rows only: never a dropped chart
    content = mask_served(content)   # an exported file never holds a card or a secret
    charts = [_masked(c) for c in list(charts)[:MAX_DOC_CHARTS]]
    return {"docx": _docx, "xlsx": _xlsx, "pdf": _pdf, "pptx": _pptx,
            "csv": _csv}[kind](content, charts)


# a table row's rule line ("|---|:--:|"): a Markdown table's, never data
_RULE_ROW = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
# a cell a spreadsheet would run as a formula when the CSV is opened (=, +, -, @, tab, CR)
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")
_NUMBER = re.compile(r"[+-]?\d+(?:[.,]\d+)*(?:\.\d+)?%?")


def _csv(content: str, charts: Sequence[Mapping[str, Any]] = ()) -> bytes:
    """One CSV row per line, cells split on "|" as an xlsx's (a Markdown table's outer
    pipes and rule row dropped), quoted as the csv module does, UTF-8 with a byte-order
    mark (Excel then reads accents right) and CRLF rows. A cell that a spreadsheet would
    run as a formula (=, +, -, @ first, not a plain number) is kept as text with a leading
    apostrophe, so opening the file never runs anything written into it."""
    import csv
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    lines = [ln for ln in content.split("\n") if ln.strip() and not _CHART_AT.match(ln)
             and not _RULE_ROW.match(ln)]
    for line in lines[:MAX_ROWS]:
        row = line.strip()
        if row.startswith("|") and row.endswith("|") and len(row) > 1:
            row = row[1:-1]
        cells = [c.strip() for c in row.split("|")][:MAX_COLS]
        writer.writerow([f"'{c}" if c.startswith(_FORMULA_START) and not _NUMBER.fullmatch(c)
                         else c for c in cells])
    return ("\ufeff" + out.getvalue()).encode("utf-8")


def _masked(chart: Mapping[str, Any]) -> dict[str, Any]:
    """A chart's words as a file holds them: no card or secret in its title, labels,
    series names or statement."""
    out = dict(chart)
    for key in ("title", "x", "sql"):
        if isinstance(out.get(key), str):
            out[key] = mask_served(out[key])
    out["labels"] = [mask_served(str(v)) for v in out.get("labels") or ()]
    out["series"] = [{**s, "name": mask_served(str(s.get("name") or ""))}
                     for s in out.get("series") or ()]
    if out.get("panels"):
        out["panels"] = [{**p, "title": mask_served(str(p.get("title") or "")),
                          "labels": [mask_served(str(v)) for v in p.get("labels") or ()]}
                         for p in out["panels"]]
    return out


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


# A recorded permission list older than this (its ``read_at``) no longer says who may
# write: a capture that stalled leaves a removed grant in force (ACT-009). The SharePoint
# capture re-reads item permissions hourly when it runs.
ACL_MAX_AGE = timedelta(hours=24)
# A read_at this far ahead of the clock is a wrong clock: not trusted either.
ACL_FUTURE_SKEW = timedelta(minutes=5)


def acl_current(acl: Mapping[str, Any], now: Optional[datetime] = None) -> bool:
    """Whether a recorded permission list was read recently enough to act on: its
    ``read_at`` (ISO instant with offset) within ``ACL_MAX_AGE`` of ``now``; no read_at,
    an unreadable one or one in the future: not current."""
    text = acl.get("read_at")
    if not isinstance(text, str) or not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d{1,6})?)?(Z|[+-]\d{2}:\d{2})", text.strip()):
        return False
    try:
        read = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except ValueError:
        return False
    now = now or datetime.now(timezone.utc)
    return now - ACL_MAX_AGE <= read <= now + ACL_FUTURE_SKEW


def _grants_held(acl: Any, identities: frozenset[str], *, inherited_only: bool,
                 now: Optional[datetime] = None) -> Optional[tuple[bool, int]]:
    """(whether a grant naming one of ``identities`` holds a write role, how many grants
    counted) from a recorded permission list (``graph_read.permission_acl``); None when
    it cannot say: not read in full, read too long ago (``acl_current``), or a grant
    without its roles (or, for ``inherited_only``, without whether it is inherited)."""
    if isinstance(acl, str):
        try:
            acl = json.loads(acl)
        except ValueError:
            return None
    if not isinstance(acl, Mapping) or acl.get("status") != "read" \
            or not acl_current(acl, now):
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


def folder_writable(folder_acl: Any, anchor_acl: Any, identities: Any, *,
                    now: Optional[datetime] = None) -> Optional[bool]:
    """Whether a person holds a write or owner role on a SharePoint folder: from the
    folder's own recorded grants, else from the grants its file (the anchor) inherits from
    it. None: not known (no list read in full or read within ``ACL_MAX_AGE``, a grant
    without its roles, a file with no inherited grants to read). Only grants naming the person's own identities count
    (their object id and addresses; a group or a sharing link never does)."""
    ids = frozenset(str(i).strip().lower() for i in identities or () if isinstance(i, str)
                    and i.strip() and not str(i).startswith(("group:", "link-scope:")))
    if not ids:
        return None
    folder = _grants_held(folder_acl, ids, inherited_only=False, now=now)
    if folder is not None:
        return folder[0]
    anchor = _grants_held(anchor_acl, ids, inherited_only=True, now=now)
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
            keep_private(directory, 0o700)

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
                target: Optional[Target], people: tuple[str, ...] | list[str] = (),
                reply_to: Optional[str] = None) -> dict[str, Any]:
        """A draft upload (``people``: the addresses the document names, cleared by the
        people exclusion before it is made; ``reply_to``: the email the document answers,
        whose reply may carry it as an attachment, ``card_actions``)."""
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
                        and r["target"] == where and r.get("reply_to") == reply_to \
                        and r["upload_id"] not in approved:
                    return r
            row = {"event": "prepared", "upload_id": "up_" + secrets.token_hex(12),
                   "reader": reader_key, "file_id": file_id, "name": name, "kind": kind,
                   "size": len(data), "digest": digest, "target": where,
                   "people": sorted({str(p).lower() for p in people})[:200], "at": _now()}
            if reply_to:
                row["reply_to"] = reply_to
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
