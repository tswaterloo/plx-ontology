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
# a table read from a statement (``make`` ``tables``): the rows a file KIND itself can hold
# (Excel's own sheet limit; None: the kind has none, a long table runs over continued slides
# or pages). No limit of the Workbench's own (owner 11-23).
SLIDE_TABLE_ROWS = 15   # rows of a table on one slide (more go on continued slides)
MAX_TABLE_ROWS: dict[str, "int | None"] = {"xlsx": 1_048_575, "csv": None, "docx": None,
                                           "pdf": None, "pptx": None}
_TABLE_AT = re.compile(r"^\s*\[table\s+([1-9][0-9]?)\]\s*$", re.I)
_HEADING_LINE = re.compile(r"^\s*#{1,3}\s+\S")
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
    base = re.sub(r"\.(docx|xlsx|pdf|pptx|csv|xls|doc)$", "", base, flags=re.I)[:80].strip() or "Document"
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


def _int_count(raw: Any) -> Optional[int]:
    """A non-negative whole record count, or None when absent or malformed."""
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw if raw >= 0 else None
    if isinstance(raw, str):
        text = raw.strip()
        if text.isdigit():
            return int(text)
    return None


def _no_em_dash(text: str) -> str:
    """Replace any em dash with a plain spaced hyphen."""
    return text.replace("\u2014", " - ")


# Executive neutral studio palette for Word, Excel, and PowerPoint exports.
_STUDIO_HEAD_INK = "0F172A"
_STUDIO_BODY_INK = "1F2937"
_STUDIO_SUB_INK = "334155"
_STUDIO_MUTED_INK = "64748B"
_STUDIO_HEAD_FILL = "F1F5F9"
_STUDIO_ZEBRA_FILL = "F8FAFC"
_STUDIO_BORDER_LIGHT = "E2E8F0"
_STUDIO_BORDER_RULE = "CBD5E1"
_STUDIO_BORDER_ACCENT = "94A3B8"

_W_NS = ('xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
         'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"')

STUDIO_DOCX_STYLES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    f'<w:styles {_W_NS}><w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:ascii="Calibri" '
    'w:hAnsi="Calibri" w:eastAsia="Calibri" w:cs="Calibri"/>'
    f'<w:color w:val="{_STUDIO_BODY_INK}"/><w:sz w:val="21"/><w:szCs w:val="21"/>'
    '<w:lang w:val="en-US"/></w:rPr></w:rPrDefault><w:pPrDefault><w:pPr>'
    '<w:spacing w:after="140" w:line="288" w:lineRule="auto"/></w:pPr></w:pPrDefault>'
    '</w:docDefaults>'
    '<w:style w:type="paragraph" w:styleId="Normal"><w:name w:val="Normal"/><w:qFormat/>'
    '<w:pPr/><w:rPr/></w:style>'
    '<w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/>'
    '<w:basedOn w:val="Normal"/><w:qFormat/><w:pPr><w:spacing w:after="200"/></w:pPr>'
    f'<w:rPr><w:b/><w:color w:val="{_STUDIO_HEAD_INK}"/><w:sz w:val="40"/></w:rPr></w:style>'
    '<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/>'
    '<w:basedOn w:val="Normal"/><w:qFormat/><w:pPr><w:keepNext/>'
    f'<w:pBdr><w:bottom w:val="single" w:sz="6" w:space="6" w:color="{_STUDIO_BORDER_LIGHT}"/>'
    '</w:pBdr><w:spacing w:before="320" w:after="140"/><w:outlineLvl w:val="0"/></w:pPr>'
    f'<w:rPr><w:b/><w:color w:val="{_STUDIO_HEAD_INK}"/><w:sz w:val="34"/></w:rPr></w:style>'
    '<w:style w:type="paragraph" w:styleId="Heading2"><w:name w:val="heading 2"/>'
    '<w:basedOn w:val="Normal"/><w:qFormat/><w:pPr><w:keepNext/>'
    '<w:spacing w:before="260" w:after="100"/><w:outlineLvl w:val="1"/></w:pPr>'
    f'<w:rPr><w:b/><w:color w:val="{_STUDIO_SUB_INK}"/><w:sz w:val="27"/></w:rPr></w:style>'
    '<w:style w:type="paragraph" w:styleId="Heading3"><w:name w:val="heading 3"/>'
    '<w:basedOn w:val="Normal"/><w:qFormat/><w:pPr><w:keepNext/>'
    '<w:spacing w:before="200" w:after="60"/><w:outlineLvl w:val="2"/></w:pPr>'
    f'<w:rPr><w:b/><w:color w:val="{_STUDIO_SUB_INK}"/><w:sz w:val="23"/></w:rPr></w:style>'
    '<w:style w:type="paragraph" w:styleId="ListBullet"><w:name w:val="List Bullet"/>'
    '<w:basedOn w:val="Normal"/><w:qFormat/><w:pPr><w:spacing w:after="80"/></w:pPr>'
    '<w:rPr/></w:style>'
    '<w:style w:type="paragraph" w:styleId="TableText"><w:name w:val="Table Text"/>'
    '<w:basedOn w:val="Normal"/><w:qFormat/><w:pPr><w:spacing w:before="30" w:after="30" '
    'w:line="252" w:lineRule="auto"/></w:pPr><w:rPr><w:sz w:val="19"/></w:rPr></w:style>'
    '<w:style w:type="table" w:styleId="PlainTable"><w:name w:val="Plain Table"/>'
    '<w:tblPr><w:tblBorders>'
    f'<w:top w:val="single" w:sz="6" w:space="0" w:color="{_STUDIO_BORDER_RULE}"/>'
    f'<w:bottom w:val="single" w:sz="6" w:space="0" w:color="{_STUDIO_BORDER_RULE}"/>'
    f'<w:insideH w:val="single" w:sz="4" w:space="0" w:color="{_STUDIO_BORDER_LIGHT}"/>'
    f'<w:left w:val="none"/><w:right w:val="none"/>'
    f'<w:insideV w:val="single" w:sz="2" w:space="0" w:color="{_STUDIO_BORDER_LIGHT}"/>'
    '</w:tblBorders><w:tblCellMar>'
    '<w:top w:w="80" w:type="dxa"/><w:bottom w:w="80" w:type="dxa"/>'
    '<w:left w:w="110" w:type="dxa"/><w:right w:w="110" w:type="dxa"/>'
    '</w:tblCellMar></w:tblPr></w:style></w:styles>'
)

STUDIO_DOCX_SECTION = (
    '<w:sectPr><w:pgSz w:w="12240" w:h="15840"/>'
    '<w:pgMar w:top="1260" w:right="1260" w:bottom="1260" w:left="1260" '
    'w:header="720" w:footer="720" w:gutter="0"/></w:sectPr>'
)

# Keeps the exact 16-entry cellXfs index contract of doc_render.XLSX_FORMATS and chart_ooxml.STYLES.
STUDIO_XLSX_STYLES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
    '<numFmts count="6">'
    '<numFmt numFmtId="164" formatCode="#,##0.00"/>'
    '<numFmt numFmtId="165" formatCode="yyyy-mm-dd"/>'
    '<numFmt numFmtId="166" formatCode="&quot;$&quot;#,##0.00;\\-&quot;$&quot;#,##0.00"/>'
    '<numFmt numFmtId="167" formatCode="0.0%"/>'
    '<numFmt numFmtId="168" formatCode="#,##0.0"/>'
    '<numFmt numFmtId="169" formatCode="#,##0.000"/>'
    '</numFmts>'
    '<fonts count="4">'
    f'<font><sz val="10.5"/><color rgb="FF{_STUDIO_BODY_INK}"/><name val="Calibri"/></font>'
    f'<font><b/><sz val="10.5"/><color rgb="FF{_STUDIO_HEAD_INK}"/><name val="Calibri"/></font>'
    f'<font><i/><sz val="9"/><color rgb="FF{_STUDIO_MUTED_INK}"/><name val="Calibri"/></font>'
    f'<font><b/><sz val="13.5"/><color rgb="FF{_STUDIO_HEAD_INK}"/><name val="Calibri"/></font>'
    '</fonts>'
    '<fills count="3">'
    '<fill><patternFill patternType="none"/></fill>'
    '<fill><patternFill patternType="gray125"/></fill>'
    f'<fill><patternFill patternType="solid"><fgColor rgb="FF{_STUDIO_HEAD_FILL}"/>'
    '<bgColor indexed="64"/></patternFill></fill>'
    '</fills>'
    '<borders count="3">'
    '<border><left/><right/><top/><bottom/><diagonal/></border>'
    f'<border><left/><right/><top style="thin"><color rgb="FF{_STUDIO_BORDER_RULE}"/></top>'
    f'<bottom style="medium"><color rgb="FF{_STUDIO_MUTED_INK}"/></bottom><diagonal/></border>'
    f'<border><left/><right/><top/><bottom style="thin"><color rgb="FF{_STUDIO_BORDER_LIGHT}"/>'
    '</bottom><diagonal/></border>'
    '</borders>'
    '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
    '<cellXfs count="16">'
    '<xf numFmtId="0" fontId="0" fillId="0" borderId="2" xfId="0" applyFont="1" applyBorder="1" '
    'applyAlignment="1"><alignment vertical="center"/></xf>'
    '<xf numFmtId="0" fontId="1" fillId="0" borderId="2" xfId="0" applyFont="1" applyBorder="1" '
    'applyAlignment="1"><alignment vertical="center"/></xf>'
    '<xf numFmtId="0" fontId="2" fillId="0" borderId="0" xfId="0" applyFont="1" '
    'applyAlignment="1"><alignment wrapText="1" vertical="top"/></xf>'
    '<xf numFmtId="164" fontId="0" fillId="0" borderId="2" xfId="0" applyNumberFormat="1" '
    'applyFont="1" applyBorder="1" applyAlignment="1"><alignment vertical="center"/></xf>'
    '<xf numFmtId="3" fontId="0" fillId="0" borderId="2" xfId="0" applyNumberFormat="1" '
    'applyFont="1" applyBorder="1" applyAlignment="1"><alignment vertical="center"/></xf>'
    '<xf numFmtId="0" fontId="1" fillId="2" borderId="1" xfId="0" applyFont="1" applyFill="1" '
    'applyBorder="1" applyAlignment="1"><alignment vertical="center" wrapText="1"/></xf>'
    '<xf numFmtId="165" fontId="0" fillId="0" borderId="2" xfId="0" applyNumberFormat="1" '
    'applyFont="1" applyBorder="1" applyAlignment="1"><alignment horizontal="left" vertical="center"/></xf>'
    '<xf numFmtId="166" fontId="0" fillId="0" borderId="2" xfId="0" applyNumberFormat="1" '
    'applyFont="1" applyBorder="1" applyAlignment="1"><alignment vertical="center"/></xf>'
    '<xf numFmtId="167" fontId="0" fillId="0" borderId="2" xfId="0" applyNumberFormat="1" '
    'applyFont="1" applyBorder="1" applyAlignment="1"><alignment vertical="center"/></xf>'
    '<xf numFmtId="0" fontId="3" fillId="0" borderId="0" xfId="0" applyFont="1" '
    'applyAlignment="1"><alignment vertical="center"/></xf>'
    '<xf numFmtId="168" fontId="0" fillId="0" borderId="2" xfId="0" applyNumberFormat="1" '
    'applyFont="1" applyBorder="1" applyAlignment="1"><alignment vertical="center"/></xf>'
    '<xf numFmtId="169" fontId="0" fillId="0" borderId="2" xfId="0" applyNumberFormat="1" '
    'applyFont="1" applyBorder="1" applyAlignment="1"><alignment vertical="center"/></xf>'
    '<xf numFmtId="0" fontId="0" fillId="0" borderId="2" xfId="0" applyFont="1" applyBorder="1"/>'
    '<xf numFmtId="164" fontId="1" fillId="0" borderId="1" xfId="0" applyNumberFormat="1" '
    'applyFont="1" applyBorder="1" applyAlignment="1"><alignment vertical="center"/></xf>'
    '<xf numFmtId="166" fontId="1" fillId="0" borderId="1" xfId="0" applyNumberFormat="1" '
    'applyFont="1" applyBorder="1" applyAlignment="1"><alignment vertical="center"/></xf>'
    '<xf numFmtId="3" fontId="1" fillId="0" borderId="1" xfId="0" applyNumberFormat="1" '
    'applyFont="1" applyBorder="1" applyAlignment="1"><alignment vertical="center"/></xf>'
    '</cellXfs></styleSheet>'
)


def _polish_docx_block_xml(xml: str) -> str:
    """Replace default blue table fills with crisp neutral studio fills and subtle zebra tint."""
    xml = xml.replace('w:fill="D9E2F3"', f'w:fill="{_STUDIO_HEAD_FILL}"')
    xml = xml.replace('w:color="8EAADB"', f'w:color="{_STUDIO_BORDER_RULE}"')
    if "<w:tbl>" not in xml:
        return xml
    row_idx = 0

    def _style_row(match: re.Match[str]) -> str:
        nonlocal row_idx
        row_xml = match.group(0)
        is_header = "<w:tblHeader/>" in row_xml
        if is_header:
            row_idx = 0
            return row_xml
        row_idx += 1
        if row_idx % 2 == 0 and "<w:shd " not in row_xml:
            row_xml = row_xml.replace(
                "</w:tcPr>",
                f'<w:shd w:val="clear" w:color="auto" w:fill="{_STUDIO_ZEBRA_FILL}"/></w:tcPr>',
            )
        return row_xml

    return re.sub(r"<w:tr>.*?</w:tr>", _style_row, xml, flags=re.S)


def _polish_xlsx_sheet_xml(xml: str) -> str:
    """Add crisp executive row heights and landscape fit-to-width print setup to a worksheet."""
    def _row_height(match: re.Match[str]) -> str:
        open_tag, inner, close_tag = match.group(1), match.group(2), match.group(3)
        if "ht=" in open_tag:
            return match.group(0)
        if 's="9"' in inner:
            ht = "26"
        elif 's="5"' in inner:
            ht = "24"
        else:
            ht = "19.5"
        return f'{open_tag[:-1]} ht="{ht}" customHeight="1">{inner}{close_tag}'

    xml = re.sub(r'(<row\s[^>]*>)(.*?)(</row>)', _row_height, xml, flags=re.S)
    if "<sheetPr>" not in xml:
        xml = xml.replace(
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">',
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<sheetPr><pageSetUpPr fitToPage="1"/></sheetPr>',
            1,
        )
    if "<pageSetup" not in xml:
        page = (
            '<pageMargins left="0.5" right="0.5" top="0.6" bottom="0.6" header="0.3" footer="0.3"/>'
            '<pageSetup orientation="landscape" fitToWidth="1" fitToHeight="0"/>'
        )
        xml = xml.replace("</worksheet>", page + "</worksheet>", 1)
    return xml


def _polish_pptx_bytes(raw_pptx: bytes, slide_count: int) -> bytes:
    """Apply crisp executive slide styling (header rule, neutral table cells, slide numbers)."""
    accent_rule = (
        '<p:sp><p:nvSpPr><p:cNvPr id="90" name="HeaderRule"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr>'
        '<p:spPr><a:xfrm><a:off x="457200" y="960000"/><a:ext cx="11277600" cy="12700"/></a:xfrm>'
        '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom>'
        f'<a:solidFill><a:srgbClr val="{_STUDIO_BORDER_LIGHT}"/></a:solidFill>'
        '<a:ln><a:noFill/></a:ln></p:spPr></p:sp>'
    )
    table_border = (
        f'<a:lnL w="6350"><a:noFill/></a:lnL><a:lnR w="6350"><a:noFill/></a:lnR>'
        f'<a:lnT w="6350"><a:solidFill><a:srgbClr val="{_STUDIO_BORDER_LIGHT}"/></a:solidFill></a:lnT>'
        f'<a:lnB w="6350"><a:solidFill><a:srgbClr val="{_STUDIO_BORDER_LIGHT}"/></a:solidFill></a:lnB>'
    )
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(raw_pptx), "r") as src, \
            zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for info in src.infolist():
            body = src.read(info.filename)
            m = re.fullmatch(r"ppt/slides/slide(\d+)\.xml", info.filename)
            if m:
                n = int(m.group(1))
                xml = body.decode("utf-8")
                xml = xml.replace('val="F3F4F6"', f'val="{_STUDIO_HEAD_FILL}"')
                xml = xml.replace(
                    "<a:tcPr>",
                    f'<a:tcPr marL="91440" marR="91440" marT="54864" marB="54864" anchor="ctr">{table_border}',
                )
                xml = xml.replace(
                    "<a:tcPr/>",
                    f'<a:tcPr marL="91440" marR="91440" marT="54864" marB="54864" anchor="ctr">{table_border}</a:tcPr>',
                )
                footer = (
                    f'<p:sp><p:nvSpPr><p:cNvPr id="91" name="SlideNumber"/>'
                    '<p:cNvSpPr txBox="1"/><p:nvPr/></p:nvSpPr>'
                    '<p:spPr><a:xfrm><a:off x="10800000" y="6460000"/><a:ext cx="934800" cy="260000"/>'
                    '</a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom><a:noFill/></p:spPr>'
                    '<p:txBody><a:bodyPr lIns="0" tIns="0" rIns="0" bIns="0"/><a:lstStyle/>'
                    '<a:p><a:pPr algn="r"/><a:r><a:rPr lang="en-US" sz="900" dirty="0">'
                    f'<a:solidFill><a:srgbClr val="{_STUDIO_MUTED_INK}"/></a:solidFill></a:rPr>'
                    f'<a:t>{n} / {slide_count}</a:t></a:r></a:p></p:txBody></p:sp>'
                )
                xml = xml.replace("</p:spTree>", accent_rule + footer + "</p:spTree>", 1)
                body = xml.encode("utf-8")
            elif info.filename.startswith("ppt/theme/theme") and info.filename.endswith(".xml"):
                xml = body.decode("utf-8").replace('val="1F2937"', f'val="{_STUDIO_HEAD_INK}"')
                body = xml.encode("utf-8")
            new_info = zipfile.ZipInfo(info.filename, _FIXED)
            new_info.compress_type = zipfile.ZIP_DEFLATED
            dst.writestr(new_info, body)
    return out.getvalue()


def _table_notes(t: Mapping[str, Any]) -> list[str]:
    if not (t.get("sql") or t.get("as_of") or t.get("measure")):
        return []
    from operating import chart_ooxml as ox
    return [_no_em_dash(ln) for ln in ox.statement_provenance(
        count=_int_count(t.get("count")),
        as_of=t.get("as_of"), sql=str(t.get("sql") or ""), measure=t.get("measure"))]


def _citation_notes(c: Mapping[str, Any]) -> list[str]:
    from operating import chart_ooxml as ox
    return [_no_em_dash(ln) for ln in ox.statement_provenance(
        count=_int_count(c.get("count")),
        as_of=c.get("as_of"), sql=str(c.get("sql") or ""), measure=c.get("measure"),
        title=str(c.get("label") or c.get("title") or ""))]


def _docx(content: str, charts: Sequence[Mapping[str, Any]] = (),
          table_notes: Mapping[int, Sequence[str]] = (),
          citations: Sequence[Sequence[str]] = ()) -> bytes:
    """A Word document of the content's blocks (``doc_render``: real heading styles, a
    styles part, letter pages, shaded header rows, numbers aligned, tracked changes for a
    redline's marks) with executive neutral studio styling, each chart a native Word chart
    where it is placed, and each cited table or figure carrying its statement."""
    from operating import chart_docs, doc_render as dr
    from operating import chart_ooxml as ox
    parts: list[str] = []
    extra: dict[str, Any] = {}
    relations: list[tuple[str, str, str]] = []
    overrides: dict[str, str] = {}
    ids = dr._Ids()
    for block in dr.blocks(content, charts):
        if block[0] == "chart":
            body, chart_parts, chart_rels, chart_types = chart_docs.docx_chart(
                block[1], len(relations) + 1)
            parts.append(body)
            extra.update(chart_parts)
            relations += chart_rels
            overrides.update(chart_types)
            continue
        if block[0] == "cite":
            lines = table_notes.get(block[1]) if isinstance(table_notes, Mapping) else ()
            if lines:
                parts.append("".join(
                    f"<w:p>{chart_docs._w_run(_no_em_dash(ln), size=16, color=chart_docs.MUTED)}</w:p>"
                    for ln in lines))
            continue
        parts.append(_polish_docx_block_xml(dr.docx_block(block, ids)))
    if citations:
        parts.append(dr.docx_block(("heading", 2, "Sources and statements"), ids))
        for entry in citations:
            parts.append("".join(
                f"<w:p>{chart_docs._w_run(_no_em_dash(ln), size=16, color=chart_docs.MUTED)}</w:p>"
                for ln in entry))
    while parts and parts[-1] == "<w:p/>":
        parts.pop()
    body = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<w:document {chart_docs.DOC_NS}><w:body>' + "".join(parts)
            + STUDIO_DOCX_SECTION + "</w:body></w:document>")
    main = "application/vnd.openxmlformats-officedocument.wordprocessingml."
    defaults = {**ox.BASE_TYPES, "xlsx": ox.CT_XLSX} if relations else dict(ox.BASE_TYPES)
    types = ox.content_types(defaults, {"/word/document.xml": main + "document.main+xml",
                                        "/word/styles.xml": main + "styles+xml",
                                        "/word/numbering.xml": main + "numbering+xml",
                                        **overrides})
    relations = relations + [("rIdStyles", "styles", "styles.xml"),
                             ("rIdNumbering", "numbering", "numbering.xml")]
    return ox.zip_parts({"[Content_Types].xml": types,
                         "_rels/.rels": _RELS.format(target="word/document.xml"),
                         "word/document.xml": body, "word/styles.xml": STUDIO_DOCX_STYLES,
                         "word/numbering.xml": dr.DOCX_NUMBERING,
                         "word/_rels/document.xml.rels": ox.rels(relations), **extra})


def _sources_rows(tables: Sequence[Mapping[str, Any]], charts: Sequence[Mapping[str, Any]],
                  citations: Sequence[Mapping[str, Any]]) -> list[list[str]]:
    """Rows for the Excel 'Sources' worksheet: every cited table, chart and prose figure
    with its record count, as-of date, governed measure and full uncut SQL statement."""
    rows: list[list[str]] = [["Item", "Records", "As of", "Measure", "Statement"]]

    def _m_text(m: Any) -> str:
        if not isinstance(m, Mapping):
            return ""
        name = str(m.get("name") or m.get("id") or "").strip()
        bits = [str(m.get(k) or "").strip() for k in ("owner", "version") if m.get(k)]
        return f"{name} ({', '.join(bits)})" if name and bits else name

    for idx, t in enumerate(tables, 1):
        if t.get("sql") or t.get("as_of") or t.get("measure"):
            cnt = _int_count(t.get("count"))
            rows.append([
                str(t.get("title") or f"Table {idx}"),
                str(cnt) if cnt is not None else "",
                str(t.get("as_of") or "")[:10],   # guard-ok: ISO date prefix YYYY-MM-DD
                _m_text(t.get("measure")),
                " ".join(str(t.get("sql") or "").split()),
            ])
    for idx, ch in enumerate(charts, 1):
        if ch.get("sql") or ch.get("as_of") or ch.get("measure"):
            rows.append([
                str(ch.get("title") or f"Chart {idx}"),
                str(ch.get("rows") or len(ch.get("labels") or ())),
                str(ch.get("as_of") or "")[:10],   # guard-ok: ISO date prefix YYYY-MM-DD
                _m_text(ch.get("measure")),
                " ".join(str(ch.get("sql") or "").split()),
            ])
    for idx, c in enumerate(citations, 1):
        cnt = _int_count(c.get("count"))
        rows.append([
            str(c.get("label") or c.get("title") or f"Figure {idx}"),
            str(cnt) if cnt is not None else "",
            str(c.get("as_of") or "")[:10],   # guard-ok: ISO date prefix YYYY-MM-DD
            _m_text(c.get("measure")),
            " ".join(str(c.get("sql") or "").split()),
        ])
    return rows


def _xlsx(content: str, charts: Sequence[Mapping[str, Any]] = (), cap: int = MAX_ROWS,
          tables: Sequence[Mapping[str, Any]] = (),
          citations: Sequence[Mapping[str, Any]] = ()) -> bytes:
    """One row per line, cells split on "|" (a Markdown table's outer pipes and rule row
    dropped), typed and formatted with executive neutral studio styling; each chart its
    rows and native chart on a sheet of its own; a 'Sources' sheet when any table or
    citation carries its statement."""
    from operating import chart_ooxml as ox, doc_render as dr
    lines = [ln for ln in content.split("\n") if ln.strip() and not _CHART_AT.match(ln)
             and not dr.TABLE_CITE_AT.match(ln) and not _RULE_ROW.match(ln)]
    rows = []
    for line in lines[:cap]:
        row = line.strip()
        if row.startswith("|") and row.endswith("|") and len(row) > 1:
            row = row[1:-1]   # a Markdown table's row: no empty first and last cell
        rows.append([c.strip() for c in row.split("|")])   # "Label:|" keeps its blank
    sheet = _polish_xlsx_sheet_xml(dr.xlsx_sheet(rows, MAX_COLS))
    sheets = [("Sheet1", {"xl/worksheets/sheet1.xml": sheet},
               {"/xl/worksheets/sheet1.xml": ox.SHEET_CT})]
    first = 1
    for n, chart in enumerate(charts, 2):
        name = f"Chart {n - 1}"
        parts, types, made = ox.sheet_parts(chart, n, first, name)
        sheets.append((name, parts, types))
        first += made
    if any(t.get("sql") or t.get("as_of") or t.get("measure") for t in tables) or citations:
        s_idx = len(sheets) + 1
        src_xml = _polish_xlsx_sheet_xml(
            dr.xlsx_sheet(_sources_rows(tables, charts, citations), MAX_COLS, literal=True)
        )
        sheets.append(("Sources", {f"xl/worksheets/sheet{s_idx}.xml": src_xml},
                       {f"/xl/worksheets/sheet{s_idx}.xml": ox.SHEET_CT}))
    return ox.workbook(sheets, styles=STUDIO_XLSX_STYLES)


def _pdf(content: str, charts: Sequence[Mapping[str, Any]] = (),
         table_notes: Mapping[int, Sequence[str]] = (),
         citations: Sequence[Sequence[str]] = ()) -> bytes:
    from operating import doc_render
    try:
        return doc_render.pdf(content, charts, table_notes=table_notes, citations=citations)
    except ImportError:
        raise FileRefused("pdf_unavailable") from None


SLIDE_BULLET_CAP = 8   # max bullets/lines on a single text slide before clean continuation


def _paginate_slide_lines(slides: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Split overcrowded text slides and keep table lead copy concise so text never overlaps."""
    out: list[dict[str, Any]] = []
    for sl in slides:
        lines = list(sl.get("lines") or ())
        has_chart = sl.get("chart") is not None
        has_table = sl.get("table") is not None
        if has_table and not has_chart and len(lines) > 2:
            # Keep at most 2 concise lead lines above a slide table; move extra bullets to notes.
            extra = lines[2:]
            notes = list(sl.get("notes") or ()) + extra
            out.append({**sl, "lines": lines[:2], "notes": notes})
            continue
        if not has_chart and not has_table and len(lines) > SLIDE_BULLET_CAP:
            title = str(sl.get("title") or "")
            for start in range(0, len(lines), SLIDE_BULLET_CAP):
                chunk = lines[start:start + SLIDE_BULLET_CAP]
                chunk_title = title if start == 0 else f"{title} (continued)".strip()
                item = {**sl, "title": chunk_title, "lines": chunk}
                if start > 0 and "notes" in item:
                    item.pop("notes", None)
                out.append(item)
            continue
        out.append(sl)
    return out


def _pptx(content: str, charts: Sequence[Mapping[str, Any]] = (),
          table_notes: Mapping[int, Sequence[str]] = (),
          citations: Sequence[Sequence[str]] = ()) -> bytes:
    """A deck: a slide per heading (its lines below it; the first lines before any heading
    a slide of their own), a slide per chart where it is placed (on its heading's slide
    when that has a few lines only), and a Markdown table a native table on its slide.
    Cited tables and figures carry their source line on the slide and full statements in
    speaker notes."""
    from operating import chart_docs, doc_render as dr
    slides: list[dict[str, Any]] = []
    current: Optional[dict[str, Any]] = None
    last_table_slides: list[dict[str, Any]] = []
    for block in dr.blocks(content, charts):
        kind = block[0]
        if kind == "cite":
            lines = list(table_notes.get(block[1]) or ()) if isinstance(table_notes, Mapping) else []
            if lines:
                for sl in last_table_slides:
                    sl["notes"] = list(sl.get("notes") or ()) + lines
            last_table_slides = []
            continue
        if kind == "chart":
            last_table_slides = []
            chart = block[1]
            if current is not None and current.get("chart") is None \
                    and not current.get("table") \
                    and len(current["lines"]) <= chart_docs.CHART_SLIDE_LINES:
                current["chart"] = chart
                current["title"] = current["title"] or str(chart.get("title") or "")
            else:
                current = {"title": str(chart.get("title") or ""), "lines": [],
                           "chart": chart}
                slides.append(current)
            continue   # the lines after it (until a heading) are that slide's too
        if kind == "heading":
            last_table_slides = []
            current = {"title": dr.plain(block[2]), "lines": []}
            slides.append(current)
            continue
        if kind == "divider":
            continue
        if current is None:
            current = {"title": "", "lines": []}
            slides.append(current)
        if kind == "table":
            rows, header = block[1], block[2] or len(block[1]) > 1
            if current.get("chart") is not None or current.get("table"):
                current = {"title": current["title"], "lines": []}
                slides.append(current)
            width = max(len(r) for r in rows)
            rows = [[dr.plain(c) for c in r] + [""] * (width - len(r)) for r in rows]
            kinds = dr.column_kinds(rows, header)
            body = [[dr.shown(c, kinds[n]) for n, c in enumerate(r)]
                    for r in (rows[1:] if header else rows)] or [[""] * width]
            title = current["title"]
            last_table_slides = []
            for start in range(0, len(body), SLIDE_TABLE_ROWS):
                if start:   # every row kept: the rest on slides of their own, header again
                    current = {"title": f"{title} (continued)".strip(), "lines": []}
                    slides.append(current)
                current["table"] = {
                    "header": rows[0] if header else [""] * width,
                    "rows": body[start:start + SLIDE_TABLE_ROWS],
                    "right": [k is not None for k in kinds],
                    "widths": [max(3, min(40, max(len(r[n]) for r in rows)))
                               for n in range(width)]}
                last_table_slides.append(current)
            continue
        last_table_slides = []
        text = dr.plain(block[2] if kind == "bullet" else block[1])
        current["lines"].append(("- " + text) if kind == "bullet" else text)
    if citations:
        summary_lines = [ln for entry in citations for ln in entry if not ln.startswith("Statement: ")]
        all_notes = [ln for entry in citations for ln in entry]
        slides.append({"title": "Sources", "lines": summary_lines or ["Statements in speaker notes."],
                       "notes": all_notes})
    slides = _paginate_slide_lines(slides)
    raw = chart_docs.pptx(slides)
    return _polish_pptx_bytes(raw, max(len(slides), 1))


def data_rows(content: str) -> int:
    """The rows of ``content`` a sheet would hold: not the blank lines, "[chart N]" or
    "[table N]" placeholders or divider lines. ``make``'s own count, so a pointer to
    tables and the refusal agree."""
    return sum(1 for ln in content.split("\n") if ln.strip() and not _CHART_AT.match(ln)
               and not _TABLE_AT.match(ln) and not _RULE_ROW.match(ln))


def _mask_measure(measure: Any) -> Any:
    if not isinstance(measure, Mapping):
        return measure
    from operating.doc_render import without_doubt_marks
    return {k: (without_doubt_marks(mask_served(v)) if isinstance(v, str) else v)
            for k, v in measure.items()}


def _masked_citation(c: Mapping[str, Any]) -> dict[str, Any]:
    from operating.doc_render import without_doubt_marks
    out = dict(c)
    for k in ("label", "title", "sql"):
        if isinstance(out.get(k), str):
            out[k] = without_doubt_marks(mask_served(out[k]))
    if isinstance(out.get("measure"), Mapping):
        out["measure"] = _mask_measure(out["measure"])
    return out


def make(kind: str, content: str, charts: Sequence[Mapping[str, Any]] = (),
         tables: Sequence[Mapping[str, Any]] = (),
         citations: Sequence[Mapping[str, Any]] = ()) -> bytes:
    """The file of ``kind`` holding ``content`` (lines; a docx reads its Markdown headings,
    bullets, bold and tables; an xlsx row's cells split on "|"; a pptx a slide per
    heading), ``charts`` (drawn charts, ``models.AskChart`` dicts, at most
    ``MAX_DOC_CHARTS``) where "[chart N]" lines place them, ``tables`` (``table``:
    every row of a statement read as the person) where "[table N]" lines place them (the
    rest at the end), and ``citations`` (statements behind prose figures). Content of more
    rows than an Excel or CSV file takes is refused, never cut."""
    if kind not in KINDS:
        raise FileRefused("kind_unknown")
    if len(content) > MAX_CONTENT or not (content.strip() or charts or tables):
        raise FileRefused("content_size")
    if kind == "csv" and charts:
        raise FileRefused("charts_not_held")   # a CSV is rows only: never a dropped chart
    if kind in ("xlsx", "csv") and data_rows(content) > MAX_ROWS:
        raise FileRefused("content_size")
    held = MAX_TABLE_ROWS[kind]
    if held is not None and any((_int_count(t.get("count")) or 0) > held for t in tables):
        raise FileRefused("table_size")
    clean_tables = [_masked_citation(t) for t in tables]
    clean_cites = [_masked_citation(c) for c in citations]
    table_notes = {i: n for i, t in enumerate(clean_tables) if (n := _table_notes(t))}
    cite_notes = [n for c in clean_cites if (n := _citation_notes(c))]
    content, cap = _with_tables(kind, content, clean_tables, table_notes)
    content = mask_served(content)   # an exported file never holds a card or a secret
    from operating.doc_render import without_doubt_marks
    content = _no_em_dash(without_doubt_marks(content))   # "(unverified)" and em dashes never enter files
    charts = [_masked(c) for c in list(charts)[:MAX_DOC_CHARTS]]
    if kind == "xlsx":
        return _xlsx(content, charts, cap, clean_tables, clean_cites)
    if kind == "csv":
        return _csv(content, charts, cap)
    return {"docx": _docx, "pdf": _pdf, "pptx": _pptx}[kind](
        content, charts, table_notes, cite_notes)


def add_excel_sheet(
    xlsx_bytes: bytes,
    sheet_name: str,
    headers: Sequence[Any],
    rows: Sequence[Any],
    *,
    title: Optional[str] = None,
    formulas: Optional[Mapping[Any, str] | Sequence[tuple[str, str]]] = None,
    number_formats: Optional[Mapping[Any, str]] = None,
    if_exists: str = "error",
) -> bytes:
    """Add a styled worksheet tab to an existing ``.xlsx`` workbook via ``doc_excel_ops``."""
    from operating import doc_excel_ops as deo
    return deo.add_sheet_to_workbook(
        xlsx_bytes,
        sheet_name,
        headers,
        rows,
        title=title,
        formulas=formulas,
        number_formats=number_formats,
        if_exists=if_exists,
    )


def append_excel_rows(
    xlsx_bytes: bytes,
    sheet_name: str,
    rows: Sequence[Any],
    *,
    match_headers: bool = True,
    create_if_missing: bool = True,
    headers: Optional[Sequence[Any]] = None,
    number_formats: Optional[Mapping[Any, str]] = None,
) -> bytes:
    """Append rows to an existing worksheet (or create it if missing) via ``doc_excel_ops``."""
    from operating import doc_excel_ops as deo
    return deo.append_rows_to_sheet(
        xlsx_bytes,
        sheet_name,
        rows,
        match_headers=match_headers,
        create_if_missing=create_if_missing,
        headers=headers,
        number_formats=number_formats,
    )


def update_excel_cells(
    xlsx_bytes: bytes,
    updates: Mapping[str, Any],
    *,
    sheet_name: Optional[str] = None,
    allow_formula_overwrite: bool = False,
) -> bytes:
    """Update named or addressed cells in an existing ``.xlsx`` workbook via ``doc_excel_ops``."""
    from operating import doc_excel_ops as deo
    return deo.update_named_cells(
        xlsx_bytes,
        updates,
        sheet_name=sheet_name,
        allow_formula_overwrite=allow_formula_overwrite,
    )


def inspect_excel(xlsx_bytes: bytes) -> dict[str, Any]:
    """Summarize sheets, headers, row counts, and defined names of an ``.xlsx`` workbook."""
    from operating import doc_excel_ops as deo
    return deo.inspect_workbook(xlsx_bytes)


def table(title: Any, columns: Sequence[Any], rows: Sequence[Sequence[Any]],
          *, sql: str = "", as_of: Any = None, measure: Optional[Mapping[str, Any]] = None
          ) -> dict[str, Any]:
    """A table for ``make``: its title, its header and rows as Markdown table lines
    (a cell's "|" and line breaks read as "/" and spaces, so no cell splits), and its
    optional statement citation (``sql``, ``as_of``, ``measure``)."""
    def cell(value: Any) -> str:
        return re.sub(r"\s*[\r\n]+\s*", " ", str(value)).replace("|", "/").strip()
    lines = ["| " + " | ".join(cell(c) for c in columns[:MAX_COLS]) + " |"]
    lines += ["| " + " | ".join(cell(c) for c in list(r)[:MAX_COLS]) + " |" for r in rows]
    out: dict[str, Any] = {"title": str(title or "").strip(), "lines": "\n".join(lines),
                           "count": len(rows)}
    if sql and str(sql).strip():
        out["sql"] = str(sql).strip()
    if as_of:
        out["as_of"] = str(as_of)
    if isinstance(measure, Mapping):
        out["measure"] = dict(measure)
    return out


def _with_tables(kind: str, content: str, tables: Sequence[Mapping[str, Any]],
                 table_notes: Mapping[int, Sequence[str]] = ()
                 ) -> tuple[str, int]:
    """``content`` with each table's lines where its "[table N]" line stands (unplaced ones
    at the end; a line naming no table dropped), and the rows an Excel or CSV file may
    then hold. A title goes above its table (a heading; in Excel a row of its own when the
    sheet holds more than the table; a CSV holds rows only). For Word, PDF and PowerPoint,
    a table with statement provenance emits a ``[table-cite i]`` marker right below it."""
    if not tables:
        return content, MAX_ROWS
    own = [ln for ln in content.split("\n") if ln.strip() and not _TABLE_AT.match(ln)]
    mark_cite = kind in ("docx", "pdf", "pptx")

    def block(idx: int, t: Mapping[str, Any], titled: bool = True) -> list[str]:
        title = str(t.get("title") or "") if titled else ""
        head = [] if not title or kind == "csv" or (kind == "xlsx" and not own and
                                                    len(tables) == 1) \
            else [title if kind == "xlsx" else f"## {title}"]
        tail = [f"[table-cite {idx}]", ""] if mark_cite and idx in table_notes else [""]
        return head + str(t.get("lines") or "").split("\n") + tail

    out, used = [], set()
    for line in content.split("\n"):
        at = _TABLE_AT.match(line)
        if at:
            n = int(at.group(1)) - 1
            if n < len(tables) and n not in used:
                used.add(n)
                said = [ln for ln in out if ln.strip()]
                # placed right under a heading: that heading is its title (never two)
                out += block(n, tables[n], not (said and _HEADING_LINE.match(said[-1])))
            continue
        out.append(line)
    for n, t in enumerate(tables):
        if n not in used:
            out += block(n, t)
    held = sum((_int_count(t.get("count")) or 0) + 3 for t in tables)
    return "\n".join(out), MAX_ROWS + held


# a table row's rule line ("|---|:--:|"): a Markdown table's, never data
_RULE_ROW = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
# a cell a spreadsheet would run as a formula when the CSV is opened (=, +, -, @, tab, CR)
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")
_NUMBER = re.compile(r"[+-]?\d+(?:[.,]\d+)*(?:\.\d+)?%?")


def _csv(content: str, charts: Sequence[Mapping[str, Any]] = (), cap: int = MAX_ROWS
         ) -> bytes:
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
    for line in lines[:cap]:
        row = line.strip()
        if row.startswith("|") and row.endswith("|") and len(row) > 1:
            row = row[1:-1]
        cells = [c.strip() for c in row.split("|")][:MAX_COLS]
        writer.writerow([f"'{c}" if c.startswith(_FORMULA_START) and not _NUMBER.fullmatch(c)
                         else c for c in cells])
    return ("\ufeff" + out.getvalue()).encode("utf-8")


def _masked(chart: Mapping[str, Any]) -> dict[str, Any]:
    """A chart's words as a file holds them: no card or secret in its title, labels,
    series names, panels, measure or statement."""
    from operating.doc_render import without_doubt_marks
    clean = lambda text: without_doubt_marks(mask_served(str(text)))
    out = dict(chart)
    for key in ("title", "x", "sql", "currency", "open_period", "first_period"):
        if isinstance(out.get(key), str):
            out[key] = clean(out[key])
    out["labels"] = [clean(v) for v in out.get("labels") or ()]
    out["series"] = [{**s, "name": clean(s.get("name") or "")}
                     for s in out.get("series") or ()]
    if isinstance(out.get("measure"), Mapping):
        out["measure"] = _mask_measure(out["measure"])
    if out.get("panels"):
        out["panels"] = [
            {
                **p,
                "title": clean(p.get("title") or ""),
                "labels": [clean(v) for v in p.get("labels") or ()],
                "series": [{**s, "name": clean(s.get("name") or "")}
                           for s in p.get("series") or ()],
            }
            for p in out["panels"]
        ]
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


__all__ = ["FileRefused", "KINDS", "LIVE_MISSING", "MEDIA", "NO_WRITE", "STUDIO_DOCX_STYLES",
           "STUDIO_XLSX_STYLES", "Target", "UploadDesk", "WRITE_UNKNOWN", "add_excel_sheet",
           "append_excel_rows", "file_name", "folder_writable", "inspect_excel", "make",
           "target_of", "update_excel_cells"]
