"""Stdlib readers for captured Office originals: DOCX/PPTX to MarkItDown-shaped
Markdown, and Outlook ``.msg`` (CFB) messages to their headers and body (attachments
are container members, enumerated by ``operating.use_case_parts.members``).

Portal converts Office files with MarkItDown (``doc_convert``), an optional package
here. Without it, ``doc_convert.convert_stdlib`` uses these readers so a captured
DOCX/PPTX still gets text, in the shape MarkItDown 0.1.x writes (the comparison is
recorded in ``DOCUMENT_UNDERSTANDING.md``):

* DOCX (mammoth + markdownify in MarkItDown): ``#`` headings from the ``heading N``
  styles, paragraphs and list blocks separated by a blank line, ``* `` / ``1. `` list
  items, tables as pipe tables (an empty header row unless the first row is a
  repeated header row), a line break as a newline, other whitespace collapsed, and
  ``_``/``*`` escaped with a backslash. Emphasis is not rendered and a hyperlink is
  its text only: neither is source text, and both would make a claim review-grade.
* PPTX (python-pptx in MarkItDown): ``<!-- Slide number: N -->`` per slide, shapes
  ordered by position (placeholders inherit it from their layout and master),
  ``# `` for the title, text frames verbatim (``\\v`` for a line break), tables as
  pipe tables with the first row as header, ``![alt](name.jpg)`` for pictures and
  ``### Notes:`` for speaker notes.

What either format holds that this reader does not convert (text boxes and
footnotes in a DOCX, charts and diagrams in a PPTX, an RTF body it cannot decode in
a ``.msg``) is named in a warning, never dropped silently. A file that is not the
format, is encrypted, malformed or over a bound raises ``OfficeFormatError`` with
an explicit code. Nothing here fetches, writes or evaluates anything.
"""

from __future__ import annotations

from datetime import datetime
from email.utils import format_datetime
from io import BytesIO
import posixpath
import re
import struct
from typing import Any, Iterator
from xml.etree import ElementTree as ET
from zipfile import BadZipFile, ZipFile, ZipInfo
import zlib

from operating import msg_reader
from operating.msg_reader import CFB_MAGIC, Cfb

MAX_PART_BYTES = 10_000_000
MAX_XML_BYTES = 50_000_000
MAX_MEMBERS = 5_000
MAX_SLIDES = 500
MAX_STYLE_DEPTH = 64

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
P = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
MC = "{http://schemas.openxmlformats.org/markup-compatibility/2006}"
PKG = "{http://schemas.openxmlformats.org/package/2006/relationships}"
_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"
_WHITESPACE = re.compile(r"[ \t\n\r\f\v]+")
# What zipfile raises for a damaged archive or member (bad CRC or header, an
# unsupported version or method, a negative offset).
ZIP_ERRORS = (BadZipFile, zlib.error, EOFError, OSError, ValueError, NotImplementedError)


class OfficeFormatError(Exception):
    """The bytes are not a readable file of the expected format; ``str()`` is the code."""


# --------------------------------------------------------------- OOXML package

class _NoDoctype(ET.TreeBuilder):
    """A tree builder that refuses a document type declaration on sight."""

    def __init__(self, prefix: str) -> None:
        super().__init__()
        self.prefix = prefix

    def doctype(self, *_args: Any) -> None:
        raise OfficeFormatError(f"{self.prefix}_dtd_not_permitted")


class _Package:
    """A bounded, read-only view of an OOXML ZIP package."""

    def __init__(self, content: bytes, prefix: str) -> None:
        self.prefix = prefix
        if content.startswith(CFB_MAGIC):
            # An encrypted OOXML file is a CFB container holding the package.
            raise OfficeFormatError(f"{prefix}_encrypted" if encrypted_ooxml(content)
                                    else f"{prefix}_not_ooxml")
        try:
            self.archive = ZipFile(BytesIO(content))
        except ZIP_ERRORS as error:
            raise OfficeFormatError(f"{prefix}_invalid_zip") from error
        members = self.archive.infolist()
        if len(members) > MAX_MEMBERS:
            raise OfficeFormatError(f"{prefix}_exceeds_bound")
        if any(item.flag_bits & 1 for item in members):
            raise OfficeFormatError(f"{prefix}_encrypted")
        self.members: dict[str, ZipInfo] = {item.filename: item for item in members}
        if len(self.members) != len(members):
            raise OfficeFormatError(f"{prefix}_duplicate_members")
        self.read_bytes = 0

    def has(self, name: str) -> bool:
        return name in self.members

    def xml(self, name: str) -> ET.Element:
        info = self.members.get(name)
        if info is None:
            raise OfficeFormatError(f"{self.prefix}_missing_part")
        if info.file_size > MAX_PART_BYTES:
            raise OfficeFormatError(f"{self.prefix}_exceeds_bound")
        try:
            with self.archive.open(info) as stream:
                raw = stream.read(MAX_PART_BYTES + 1)
        except ZIP_ERRORS as error:
            raise OfficeFormatError(f"{self.prefix}_invalid_zip") from error
        self.read_bytes += len(raw)
        if len(raw) > MAX_PART_BYTES or self.read_bytes > MAX_XML_BYTES:
            raise OfficeFormatError(f"{self.prefix}_exceeds_bound")
        # The parser refuses a DTD in whatever encoding the part declares (a byte
        # search would miss UTF-16), so no entity is ever expanded.
        try:
            return ET.fromstring(raw, parser=ET.XMLParser(target=_NoDoctype(self.prefix)))
        except ET.ParseError as error:
            raise OfficeFormatError(f"{self.prefix}_invalid_xml") from error

    def rels(self, part: str) -> dict[str, tuple[str, str]]:
        """``{rId: (type suffix, part name)}`` of a part's internal relationships."""
        folder, name = posixpath.split(part)
        rels_name = posixpath.join(folder, "_rels", name + ".rels")
        if not self.has(rels_name):
            return {}
        found = {}
        for relation in self.xml(rels_name).iter(PKG + "Relationship"):
            target = relation.get("Target") or ""
            if relation.get("TargetMode") == "External" or not target:
                continue
            path = (posixpath.normpath(target.lstrip("/")) if target.startswith("/")
                    else posixpath.normpath(posixpath.join(folder, target)))
            kind = (relation.get("Type") or "").removeprefix(_REL)
            found[relation.get("Id") or ""] = (kind, path)
        return found

    def related(self, part: str, kind: str) -> str | None:
        return next((path for rel_kind, path in self.rels(part).values()
                     if rel_kind == kind and self.has(path)), None)

    def main_part(self, kind: str = "officeDocument") -> str:
        part = self.related("", kind) if self.has("_rels/.rels") else None
        if part is None:
            raise OfficeFormatError(f"{self.prefix}_missing_part")
        return part


def _collapse(text: str) -> str:
    return _WHITESPACE.sub(" ", text).strip()


def _escape(text: str) -> str:
    """markdownify's default escaping, as MarkItDown applies it: ``_`` and ``*``."""
    return text.replace("_", "\\_").replace("*", "\\*")


def _pipe_table(rows: list[list[str]], header: list[str] | None) -> str:
    """markdownify's table: a header row (empty when the source has none), a
    ``---`` row, then the body rows, each ``| a | b |``."""
    width = len(header) if header is not None else max((len(row) for row in rows), default=0)
    head = header if header is not None else [""] * width
    lines = ["| " + " | ".join(head) + " |", "| " + " | ".join(["---"] * width) + " |"]
    lines += ["| " + " | ".join(row) + " |" for row in rows]
    return "\n".join(lines)


# ------------------------------------------------------------------------ DOCX

class _Docx:
    def __init__(self, package: _Package) -> None:
        self.package = package
        self.part = package.main_part()
        self.warnings: list[str] = []
        self.styles: dict[str, ET.Element] = {}
        styles = package.related(self.part, "styles")
        if styles:
            for style in package.xml(styles).iter(W + "style"):
                if style.get(W + "type") == "paragraph" and style.get(W + "styleId"):
                    self.styles[style.get(W + "styleId")] = style
        self.formats: dict[tuple[str, str], str] = {}
        # styleId -> (numId, ilvl, chain depth) through its basedOn chain, resolved once.
        self.style_numbering: dict[str, tuple[str | None, str | None, int]] = {}
        numbering = package.related(self.part, "numbering")
        if numbering:
            self._read_numbering(package.xml(numbering))

    def _read_numbering(self, root: ET.Element) -> None:
        abstract = {}
        for item in root.findall(W + "abstractNum"):
            abstract[item.get(W + "abstractNumId")] = {
                level.get(W + "ilvl"): (level.find(W + "numFmt").get(W + "val")
                                        if level.find(W + "numFmt") is not None else "")
                for level in item.findall(W + "lvl")}
        for num in root.findall(W + "num"):
            ref = num.find(W + "abstractNumId")
            levels = dict(abstract.get(ref.get(W + "val") if ref is not None else None, {}))
            for override in num.findall(W + "lvlOverride"):
                level = override.find(W + "lvl")
                fmt = level.find(W + "numFmt") if level is not None else None
                if fmt is not None:
                    levels[override.get(W + "ilvl")] = fmt.get(W + "val")
            for ilvl, fmt in levels.items():
                self.formats[(num.get(W + "numId"), ilvl)] = fmt or ""

    @staticmethod
    def _numpr(ppr: ET.Element | None) -> tuple[str | None, str | None]:
        numpr = ppr.find(W + "numPr") if ppr is not None else None
        if numpr is None:
            return None, None
        num_id, ilvl = numpr.find(W + "numId"), numpr.find(W + "ilvl")
        return (num_id.get(W + "val") if num_id is not None else None,
                ilvl.get(W + "val") if ilvl is not None else None)

    def _style_numbering(self, style_id: str | None) -> tuple[str | None, str | None]:
        """(numId, ilvl) a style sets or inherits through ``basedOn``. Each style is
        resolved once per document; a cycle ends the chain and a chain deeper than
        ``MAX_STYLE_DEPTH`` is over a bound."""
        chain: list[str] = []
        seen: set[str] = set()
        while (style_id and style_id in self.styles and style_id not in self.style_numbering
               and style_id not in seen):
            if len(chain) == MAX_STYLE_DEPTH:
                raise OfficeFormatError("docx_exceeds_bound")
            chain.append(style_id)
            seen.add(style_id)
            based = self.styles[style_id].find(W + "basedOn")
            style_id = based.get(W + "val") if based is not None else None
        num_id, ilvl, depth = self.style_numbering.get(style_id or "", (None, None, 0))
        for current in reversed(chain):
            own_num, own_ilvl = self._numpr(self.styles[current].find(W + "pPr"))
            depth += 1
            if depth > MAX_STYLE_DEPTH:
                raise OfficeFormatError("docx_exceeds_bound")
            num_id = own_num if own_num is not None else num_id
            ilvl = own_ilvl if own_ilvl is not None else ilvl
            self.style_numbering[current] = (num_id, ilvl, depth)
        return num_id, ilvl

    def _heading(self, style_id: str | None) -> int | None:
        style = self.styles.get(style_id or "")
        name = style.find(W + "name") if style is not None else None
        label = (name.get(W + "val") or "") if name is not None else ""
        match = re.fullmatch(r"heading ([1-6])", label.strip().lower())
        return int(match.group(1)) if match else None

    def _list(self, ppr: ET.Element | None, style_id: str | None) -> tuple[bool, int] | None:
        """(ordered, level) of a numbered/bulleted paragraph, from it or its style."""
        num_id, ilvl = self._numpr(ppr)
        if num_id is None or ilvl is None:
            style_num, style_ilvl = self._style_numbering(style_id)
            num_id = style_num if num_id is None else num_id
            ilvl = style_ilvl if ilvl is None else ilvl
        if num_id is None or num_id == "0":
            return None
        if not re.fullmatch(r"[0-9]+", ilvl or ""):
            ilvl = "0"
        elif not re.fullmatch(r"0*[0-8]", ilvl):
            return None  # OOXML numbering levels are 0..8; a deeper one is invalid
        level = int(ilvl[-1])
        fmt = self.formats.get((num_id, str(level)))
        # A level the numbering part does not define is no list (as in mammoth).
        return None if fmt is None else (fmt != "bullet", level)

    def _runs(self, element: ET.Element, out: list[str]) -> None:
        for child in element:
            tag = child.tag
            if tag in (W + "del", W + "moveFrom", W + "instrText", W + "delText"):
                continue
            if tag == W + "t":
                out.append(child.text or "")
            elif tag in (W + "tab", W + "ptab"):
                out.append("\t")
            elif tag in (W + "br", W + "cr"):
                if child.get(W + "type") in (None, "textWrapping"):
                    out.append("\n")
            elif tag == W + "noBreakHyphen":
                out.append("-")
            elif tag == W + "txbxContent":
                self._warn("docx_text_boxes_not_converted")
            elif tag == W + "footnoteReference" or tag == W + "endnoteReference":
                self._warn("docx_notes_not_converted")
            elif tag == MC + "AlternateContent":
                fallback = child.find(MC + "Fallback")
                if fallback is not None:
                    self._runs(fallback, out)
            else:
                self._runs(child, out)

    def _warn(self, code: str) -> None:
        if code not in self.warnings:
            self.warnings.append(code)

    def _text(self, paragraph: ET.Element) -> str:
        pieces: list[str] = []
        self._runs(paragraph, pieces)
        lines = [_collapse(line) for line in "".join(pieces).split("\n")]
        return _escape("\n".join(lines).strip())

    def _table(self, table: ET.Element) -> str:
        rows, header = [], None
        for index, row in enumerate(table.findall(W + "tr")):
            cells = []
            for cell in row.findall(W + "tc"):
                texts = [self._text(p) for p in cell.iter(W + "p")]
                cells.append("  ".join(text.replace("\n", " ") for text in texts if text))
            trpr = row.find(W + "trPr")
            if index == 0 and trpr is not None and trpr.find(W + "tblHeader") is not None:
                header = cells
            else:
                rows.append(cells)
        return _pipe_table(rows, header)

    def _blocks(self, container: ET.Element) -> Iterator[tuple[str, Any]]:
        for child in container:
            if child.tag in (W + "sdt", W + "customXml"):
                content = child.find(W + "sdtContent") if child.tag == W + "sdt" else child
                if content is not None:
                    yield from self._blocks(content)
            elif child.tag == W + "tbl":
                yield "table", self._table(child)
            elif child.tag == W + "p":
                ppr = child.find(W + "pPr")
                style = ppr.find(W + "pStyle") if ppr is not None else None
                style_id = style.get(W + "val") if style is not None else None
                text = self._text(child)
                if not text:
                    continue
                listed = self._list(ppr, style_id)
                level = self._heading(style_id)
                if level is not None:
                    yield "block", "#" * level + " " + text
                elif listed is not None:
                    yield "item", (listed, text)
                else:
                    yield "block", text

    def markdown(self) -> str:
        root = self.package.xml(self.part)
        if root.tag != W + "document" or root.find(W + "body") is None:
            raise OfficeFormatError("docx_not_wordprocessingml")
        blocks: list[str] = []
        items: list[str] = []
        list_kind: bool | None = None
        counters: dict[int, int] = {}
        # markdownify indents a nested item by its parent's marker width and
        # cycles bullets "*", "+", "-" by depth.
        widths: dict[int, int] = {}

        def close() -> None:
            nonlocal list_kind
            if items:
                blocks.append("\n".join(items))
                items.clear()
            counters.clear()
            widths.clear()
            list_kind = None

        for kind, value in self._blocks(root.find(W + "body")):
            if kind != "item":
                close()
                blocks.append(value)
                continue
            (ordered, level), text = value
            if level == 0 and list_kind is not None and list_kind != ordered:
                close()
            if level == 0:
                list_kind = ordered
            for deeper in [key for key in counters if key > level]:
                del counters[deeper], widths[deeper]
            counters[level] = counters.get(level, 0) + 1
            marker = f"{counters[level]}. " if ordered else "*+-"[level % 3] + " "
            widths[level] = len(marker)
            indent = sum(widths.get(parent, 0) for parent in range(level))
            items.append(" " * indent + marker + text.replace("\n", " "))
        close()
        return "\n\n".join(blocks)


def docx_markdown(content: bytes) -> tuple[str, tuple[str, ...]]:
    """(MarkItDown-shaped Markdown, warnings) of a DOCX. Raises ``OfficeFormatError``."""
    package = _Package(content, "docx")
    with package.archive:
        try:
            reader = _Docx(package)
            text = reader.markdown()
        except RecursionError as error:
            raise OfficeFormatError("docx_exceeds_bound") from error
    return text, tuple(reader.warnings)


# ------------------------------------------------------------------------ PPTX

_MASTER_TYPE = {"ctrTitle": "title", "title": "title", "dt": "dt", "ftr": "ftr",
                "sldNum": "sldNum"}


def _placeholder(shape: ET.Element) -> ET.Element | None:
    for path in (P + "nvSpPr/", P + "nvPicPr/", P + "nvGraphicFramePr/", P + "nvGrpSpPr/"):
        found = shape.find(path + P + "nvPr/" + P + "ph")
        if found is not None:
            return found
    return None


def _offset(shape: ET.Element) -> tuple[int, int] | None:
    for path in (P + "spPr/" + A + "xfrm/" + A + "off", P + "xfrm/" + A + "off",
                 P + "grpSpPr/" + A + "xfrm/" + A + "off"):
        found = shape.find(path)
        if found is not None:
            try:
                return int(found.get("y") or 0), int(found.get("x") or 0)
            except ValueError as error:
                raise OfficeFormatError("pptx_invalid_xml") from error
    return None


def _frame_text(body: ET.Element) -> str:
    """python-pptx ``TextFrame.text``: paragraphs joined by ``\\n``, a break as ``\\v``."""
    paragraphs = []
    for paragraph in body.findall(A + "p"):
        pieces = []
        for child in paragraph:
            if child.tag in (A + "r", A + "fld"):
                node = child.find(A + "t")
                pieces.append(node.text or "" if node is not None else "")
            elif child.tag == A + "br":
                pieces.append("\v")
        paragraphs.append("".join(pieces))
    return "\n".join(paragraphs)


class _Pptx:
    def __init__(self, package: _Package) -> None:
        self.package = package
        self.part = package.main_part()
        self.warnings: list[str] = []
        self._layouts: dict[str, tuple[dict, dict]] = {}

    def _warn(self, code: str) -> None:
        if code not in self.warnings:
            self.warnings.append(code)

    def _positions(self, part: str) -> tuple[dict, dict]:
        """A layout's placeholder offsets by idx, and its master's by type."""
        if part not in self._layouts:
            by_idx: dict[str, tuple[int, int] | None] = {}
            by_type: dict[str, tuple[int, int] | None] = {}
            master = self.package.related(part, "slideMaster")
            for name, table, key in ((part, by_idx, "idx"), (master, by_type, "type")):
                if name is None:
                    continue
                for shape in self.package.xml(name).iter():
                    ph = _placeholder(shape)
                    if ph is None:
                        continue
                    value = ph.get(key) or ("0" if key == "idx" else "obj")
                    table.setdefault(value, _offset(shape))
                    if key == "idx":
                        table.setdefault("type:" + value, ph.get("type") or "obj")
            self._layouts[part] = (by_idx, by_type)
        return self._layouts[part]

    def _position(self, shape: ET.Element, layout: str | None) -> tuple[float, float]:
        found = _offset(shape)
        ph = _placeholder(shape)
        if found is None and ph is not None and layout is not None:
            by_idx, by_type = self._positions(layout)
            idx = ph.get("idx") or "0"
            found = by_idx.get(idx)
            if found is None:
                kind = by_idx.get("type:" + idx) or ph.get("type") or "obj"
                found = by_type.get(_MASTER_TYPE.get(kind, "body"))
        if found is None:
            return float("-inf"), float("-inf")
        return float(found[0]), float(found[1])

    def _ordered(self, shapes: list[ET.Element], layout: str | None) -> list[ET.Element]:
        return sorted(shapes, key=lambda shape: self._position(shape, layout))

    def _shape(self, shape: ET.Element, title: ET.Element | None, layout: str | None) -> str:
        out = ""
        tag = shape.tag
        if tag == P + "pic":
            props = shape.find(P + "nvPicPr/" + P + "cNvPr")
            name = props.get("name") or "" if props is not None else ""
            alt = (props.get("descr") or "" if props is not None else "") or name
            alt = _WHITESPACE.sub(" ", re.sub(r"[\r\n\[\]]", " ", alt)).strip()
            out += "\n![" + alt + "](" + re.sub(r"\W", "", name) + ".jpg)\n"
        elif tag == P + "graphicFrame":
            data = shape.find(A + "graphic/" + A + "graphicData")
            table = data.find(A + "tbl") if data is not None else None
            if table is not None:
                rows = [[_escape(_collapse(_frame_text(cell.find(A + "txBody"))
                                           if cell.find(A + "txBody") is not None else ""))
                         for cell in row.findall(A + "tc")]
                        for row in table.findall(A + "tr")]
                if rows:
                    out += _pipe_table(rows[1:], rows[0]) + "\n"
            elif data is not None and (data.get("uri") or "").endswith("/chart"):
                self._warn("pptx_charts_not_converted")
            else:
                self._warn("pptx_graphics_not_converted")
        elif tag == P + "sp" and shape.find(P + "txBody") is not None:
            text = _frame_text(shape.find(P + "txBody"))
            if shape is title:
                if text.strip():
                    out += "# " + text.lstrip() + "\n"
            else:
                out += text + "\n"
        elif tag == P + "grpSp":
            children = [child for child in shape
                        if child.tag in (P + "sp", P + "pic", P + "graphicFrame", P + "grpSp")]
            for child in self._ordered(children, None):
                out += self._shape(child, title, None)
        return out

    def markdown(self) -> str:
        presentation = self.package.xml(self.part)
        if presentation.tag != P + "presentation":
            raise OfficeFormatError("pptx_not_presentationml")
        rels = self.package.rels(self.part)
        listed = presentation.findall(P + "sldIdLst/" + P + "sldId")
        if len(listed) > MAX_SLIDES:
            raise OfficeFormatError("pptx_exceeds_bound")
        md = ""
        for number, entry in enumerate(listed, start=1):
            kind, part = rels.get(entry.get(R + "id") or "", ("", ""))
            if kind != "slide" or not self.package.has(part):
                raise OfficeFormatError("pptx_missing_part")
            md += f"\n\n<!-- Slide number: {number} -->\n"
            slide = self.package.xml(part)
            tree = slide.find(P + "cSld/" + P + "spTree")
            shapes = [child for child in (tree if tree is not None else ())
                      if child.tag in (P + "sp", P + "pic", P + "graphicFrame", P + "grpSp",
                                       P + "cxnSp")]
            # python-pptx's ``shapes.title``: the first top-level title placeholder.
            title = next((shape for shape in shapes if shape.tag == P + "sp" and
                          (ph := _placeholder(shape)) is not None and
                          ph.get("type") in ("title", "ctrTitle")), None)
            layout = self.package.related(part, "slideLayout")
            for shape in self._ordered(shapes, layout):
                md += self._shape(shape, title, layout)
            md = md.strip()
            notes = self.package.related(part, "notesSlide")
            if notes:
                body = next((shape.find(P + "txBody")
                             for shape in self.package.xml(notes).iter(P + "sp")
                             if (ph := _placeholder(shape)) is not None and
                             ph.get("type") == "body" and shape.find(P + "txBody") is not None),
                            None)
                text = _frame_text(body) if body is not None else ""
                if text.strip():
                    md = (md + "\n\n### Notes:\n" + text).strip()
        return md.strip()


def pptx_markdown(content: bytes) -> tuple[str, tuple[str, ...]]:
    """(MarkItDown-shaped Markdown, warnings) of a PPTX. Raises ``OfficeFormatError``."""
    package = _Package(content, "pptx")
    with package.archive:
        try:
            reader = _Pptx(package)
            text = reader.markdown()
        except RecursionError as error:
            raise OfficeFormatError("pptx_exceeds_bound") from error
    return text, tuple(reader.warnings)


# ------------------------------------------------------- Outlook .msg (msg_reader)
# The compound file and its MAPI properties are decoded by ``operating.msg_reader``,
# the repo's one reader; this section only picks what the ingestion path shows.

def encrypted_ooxml(content: bytes) -> bool:
    """Whether the bytes are a password-protected OOXML file (a CFB container
    holding the encrypted package, [MS-OFFCRYPTO])."""
    if not content.startswith(CFB_MAGIC):
        return False
    try:
        cfb = Cfb(content)
        return any(cfb.entries[i][0] == "EncryptedPackage" for i in cfb.children(0))
    except (ValueError, struct.error):
        return False


class _MapiStorage:
    """The properties of one MAPI object (the message, or one attachment)."""

    def __init__(self, cfb: Cfb, index: int, header: int) -> None:
        self.cfb = cfb
        self.names = {cfb.entries[i][0]: i for i in cfb.children(index)}
        props = self.names.get(msg_reader.PROPERTIES)
        self.fixed = ({} if props is None else
                      msg_reader.property_table(self._stream(props), header))

    def _stream(self, index: int) -> bytes:
        return self.cfb.stream(index, len(self.cfb.data))

    def long(self, prop: int) -> int | None:
        raw = self.fixed.get((prop << 16) | 0x0003)
        return struct.unpack("<I", raw[:4])[0] if raw else None

    def time(self, prop: int) -> datetime | None:
        raw = self.fixed.get((prop << 16) | 0x0040)
        return msg_reader.filetime(raw) if raw else None

    def binary(self, prop: int) -> bytes | None:
        index = self.names.get(f"__substg1.0_{prop:04X}0102")
        return self._stream(index) if index is not None else None

    def string(self, prop: int, codepage: str) -> str | None:
        for kind in ("001F", "001E"):
            index = self.names.get(f"__substg1.0_{prop:04X}{kind}")
            if index is not None:
                return msg_reader.string_value(self._stream(index), kind, codepage)
        return None


def _codepage(value: int | None) -> str:
    import codecs
    name = {65001: "utf-8", 1200: "utf-16-le", 20127: "ascii", 28591: "latin-1"}.get(
        value or 0, f"cp{value}" if value else "cp1252")
    try:
        return codecs.lookup(name).name
    except LookupError:
        return "cp1252"


def _rtf_body(compressed: bytes) -> tuple[str, bool] | None:
    """``(text, is_html)`` of a compressed RTF body, ``None`` when it does not decode."""
    try:
        text, html = msg_reader.rtf_text(msg_reader.rtf_decompress(compressed, MAX_PART_BYTES))
    except (ValueError, struct.error, RecursionError):
        return None
    return (text, True) if html else (text.strip(), False)


def read_msg(content: bytes) -> dict[str, Any]:
    """Headers and body of an Outlook ``.msg``; raises ``OfficeFormatError``.

    ``{"headers": [(name, value)], "body": str, "html": bool, "warnings": [...]}``.
    The body is PR_BODY, else the HTML body, else the compressed RTF body as text
    (or as the HTML it encapsulates; ``html`` tells the caller to reduce it to
    text); an RTF body that does not decode is the warning
    ``msg_rtf_body_unreadable``.
    """
    if len(content) < 512 or not content.startswith(CFB_MAGIC):
        raise OfficeFormatError("msg_not_compound_file")
    try:
        cfb = Cfb(content)
        root = {cfb.entries[i][0]: i for i in cfb.children(0)}
        if msg_reader.PROPERTIES not in root or not any(
                name.startswith("__substg1.0_") for name in root):
            raise OfficeFormatError("msg_not_outlook_message")
        message = _MapiStorage(cfb, 0, msg_reader.HEADER_TOP)
        codepage = _codepage(message.long(0x3FFD) or message.long(0x3FDE))
        warnings: list[str] = []
        sender = message.string(0x0C1F, codepage) or message.string(0x5D01, codepage)
        name = message.string(0x0C1A, codepage)
        headers = []
        if message.string(0x0037, codepage):
            headers.append(("Subject", message.string(0x0037, codepage)))
        if sender or name:
            headers.append(("From", f"{name} <{sender}>" if name and sender and name != sender
                            else sender or name))
        for label, prop in (("To", 0x0E04), ("Cc", 0x0E03)):
            if message.string(prop, codepage):
                headers.append((label, message.string(prop, codepage)))
        sent = message.time(0x0039) or message.time(0x0E06)
        if sent is not None:
            headers.append(("Date", format_datetime(sent)))
        body, html = message.string(0x1000, codepage), False
        if body is None:
            raw = message.binary(0x1013)
            if raw is None and message.string(0x1013, codepage) is not None:
                body, html = message.string(0x1013, codepage), True
            elif raw is not None:
                body, html = raw.decode(_codepage(message.long(0x3FDE)), "replace"), True
            elif (rtf := message.binary(0x1009)) is not None:
                decoded = _rtf_body(rtf)
                if decoded is None:
                    warnings.append("msg_rtf_body_unreadable")
                else:
                    body, html = decoded
    except (ValueError, OverflowError, struct.error) as error:  # OverflowError: a stream
        raise OfficeFormatError("msg_invalid_compound_file") from error  # past the file
    return {"headers": headers, "body": body or "", "html": html, "warnings": warnings}


__all__ = ["OfficeFormatError", "ZIP_ERRORS", "docx_markdown", "encrypted_ooxml",
           "pptx_markdown", "read_msg"]
