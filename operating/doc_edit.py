"""One edit of a document's own content, as a diff before anything is written: the general
document edit Ask prepares (``plx.edit_document``) over a SharePoint or OneDrive file.

``edit(name, data, change)`` takes the document's bytes as read and one change:

* ``{"find": ..., "replace": ...}`` in a Word document (``.docx``: the text of its
  paragraphs, headers and footers; a match may span runs: the runs it covers are cut
  so the text around it keeps its formatting) or a plain text file (``.txt``, ``.md``,
  ``.csv``: UTF-8);
* ``{"sheet": ..., "cell": "B7", "value": ...}`` in an Excel workbook (``.xlsx``): one
  existing cell's value (a number stays a number, text is written inline; a formula cell
  is refused, so is a cell the sheet does not hold).

The text to find must occur exactly once (none or several: refused, never guessed). It
returns the new bytes and the diff the person confirms (``field``, ``before``, ``after``:
the paragraph or cell as it is and as it would be). Every other part of the file is copied
byte for byte. ``Refused`` says why in plain words. Nothing here reads or writes a system.
"""
from __future__ import annotations

from html import escape, unescape
import io
import re
from typing import Any, Mapping
import zipfile

TEXT_KINDS = {".txt": "text/plain", ".md": "text/markdown", ".csv": "text/csv"}
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
MAX_TEXT = 4000


class Refused(ValueError):
    pass


def kind_of(name: str) -> tuple[str, str]:
    ext = ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""
    if ext == ".docx":
        return ext, DOCX
    if ext == ".xlsx":
        return ext, XLSX
    if ext in TEXT_KINDS:
        return ext, TEXT_KINDS[ext]
    raise Refused(f"Only Word (.docx), Excel (.xlsx) and text files (.txt, .md, .csv) can "
                  f"be edited here, not {ext or 'this file'}.")


def _words(value: Any, what: str, empty: bool = False) -> str:
    if not isinstance(value, str) or (not value and not empty) or len(value) > MAX_TEXT \
            or "\x00" in value:
        raise Refused(f"The {what} must be text of up to {MAX_TEXT} characters.")
    return value


def edit(name: str, data: bytes, change: Mapping[str, Any]) -> tuple[bytes, list[dict[str, Any]]]:
    ext, _kind = kind_of(name)
    if ext == ".xlsx":
        if set(change) != {"sheet", "cell", "value"}:
            raise Refused("An Excel edit names the sheet, the cell and its new value.")
        return _xlsx(data, change)
    if set(change) != {"find", "replace"}:
        raise Refused("A document edit names the text to find and its replacement.")
    find = _words(change["find"], "text to find")
    replace = _words(change["replace"], "replacement", empty=True)
    if ext == ".docx":
        return _docx(data, find, replace)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise Refused("The file is not UTF-8 text.") from None
    count = text.count(find)
    _once(count, find)
    at = text.index(find)
    line_start = text.rfind("\n", 0, at) + 1
    line_end = text.find("\n", at + len(find))
    line_end = len(text) if line_end < 0 else line_end
    before = text[line_start:line_end]
    after = before.replace(find, replace, 1)
    new = text[:line_start] + after + text[line_end:]
    return new.encode("utf-8"), [{"field": f"line {text.count(chr(10), 0, at) + 1}",
                                  "before": before, "after": after}]


def text_of(name: str, data: bytes) -> str:
    """All the text of the document as a reader sees it (every paragraph of the body,
    headers and footers; every sheet name, shared and inline string and value; a text
    file whole), so the caller filters it before any part of it reaches the model.
    Raises ``Refused`` when it cannot be read whole (never half-filtered)."""
    ext, _kind = kind_of(name)
    if ext in TEXT_KINDS:
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            raise Refused("The file is not UTF-8 text.") from None
    out: list[str] = []
    with _open(data) as z:
        for part in z.namelist():
            if ext == ".docx" and _DOCX_PARTS.fullmatch(part) or ext == ".docx" and \
                    re.fullmatch(r"word/(footnotes|endnotes|comments)\.xml", part):
                xml = z.read(part).decode("utf-8", "replace")
                for para in _PARA.finditer(xml):
                    out.append("".join(unescape(r.group(2))
                                       for r in _RUN_TEXT.finditer(para.group(0))))
            elif ext == ".xlsx" and (part in ("xl/sharedStrings.xml", "xl/workbook.xml")
                                     or re.fullmatch(r"xl/(worksheets|comments)[^/]*/?.*\.xml",
                                                     part)):
                xml = z.read(part).decode("utf-8", "replace")
                out += [unescape(t) for t in re.findall(r"<(?:t|v)(?:\s[^>]*)?>(.*?)</(?:t|v)>",
                                                        xml, re.S)]
                out += [unescape(n) for n in re.findall(r'<sheet\s[^>]*name="([^"]*)"', xml)]
    return "\n".join(out)


def _once(count: int, find: str) -> None:
    if count == 0:
        raise Refused(f"\"{find[:80]}\" is not in the document as read.")
    if count > 1:
        raise Refused(f"\"{find[:80]}\" occurs {count} times: quote more of it so it is "
                      "one place.")


def _rezip(data: bytes, parts: Mapping[str, bytes]) -> bytes:
    """The package with ``parts`` replaced, every other entry copied as it was."""
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as src, \
            zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for info in src.infolist():
            body = parts.get(info.filename, None)
            dst.writestr(info, src.read(info.filename) if body is None else body)
    return out.getvalue()


def _open(data: bytes) -> zipfile.ZipFile:
    try:
        return zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise Refused("The file is not a readable Office document.") from None


_PARA = re.compile(r"<w:p[ >].*?</w:p>|<w:p/>", re.S)
_RUN_TEXT = re.compile(r"(<w:t(?:\s[^>]*)?>)(.*?)(</w:t>)", re.S)
_DOCX_PARTS = re.compile(r"word/(document|header\d*|footer\d*)\.xml")


def _docx(data: bytes, find: str, replace: str) -> tuple[bytes, list[dict[str, Any]]]:
    with _open(data) as z:
        names = [n for n in z.namelist() if _DOCX_PARTS.fullmatch(n)]
        xml = {n: z.read(n).decode("utf-8") for n in names}
    hits = []
    for part, text in xml.items():
        for para in _PARA.finditer(text):
            runs = list(_RUN_TEXT.finditer(para.group(0)))
            words = "".join(unescape(r.group(2)) for r in runs)
            hits += [(part, para, runs, words)] * words.count(find)
    _once(len(hits), find)
    part, para, runs, words = hits[0]
    start = words.index(find)
    end = start + len(find)
    out, pos, offset = [], 0, 0
    body = para.group(0)
    for r in runs:
        piece = unescape(r.group(2))
        lo, hi = offset, offset + len(piece)
        offset = hi
        if hi <= start or lo >= end:
            continue
        # the first run the match covers carries the replacement; the text before and
        # after the match stays in its own run (its formatting kept)
        keep = (piece[:start - lo] if lo < start else "") + (replace if not out else "") \
            + (piece[end - lo:] if hi > end else "")
        opening = r.group(1)
        if keep != keep.strip() and "xml:space" not in opening:
            opening = opening[:-1] + ' xml:space="preserve">'
        out.append(body[pos:r.start()] + opening + escape(keep, quote=False) + r.group(3))
        pos = r.end()
    new_para = "".join(out) + body[pos:]
    text = xml[part]
    xml_new = text[:para.start()] + new_para + text[para.end():]
    after = words[:start] + replace + words[end:]
    return _rezip(data, {part: xml_new.encode("utf-8")}), [
        {"field": "paragraph" if part == "word/document.xml" else part.split("/")[1][:-4],
         "before": words, "after": after}]


_CELL = re.compile(r"^[A-Z]{1,3}[1-9][0-9]{0,6}$")
_NUMBER = re.compile(r"^-?(?:0|[1-9][0-9]{0,14})(?:\.[0-9]{1,10})?$")


def _sheet_part(z: zipfile.ZipFile, sheet: str) -> str:
    book = z.read("xl/workbook.xml").decode("utf-8")
    rels = z.read("xl/_rels/workbook.xml.rels").decode("utf-8")
    for tag in re.finditer(r"<sheet\s[^>]*/?>", book):
        attrs = dict(re.findall(r'([\w:]+)="([^"]*)"', tag.group(0)))
        if unescape(attrs.get("name", "")) == sheet:
            rid = attrs.get("r:id")
            for rel in re.finditer(r"<Relationship\s[^>]*/?>", rels):
                ra = dict(re.findall(r'([\w:]+)="([^"]*)"', rel.group(0)))
                if ra.get("Id") == rid:
                    target = ra.get("Target", "").lstrip("/")
                    return target if target.startswith("xl/") else "xl/" + target
    raise Refused(f"The workbook has no sheet named \"{sheet[:60]}\".")


def _xlsx(data: bytes, change: Mapping[str, Any]) -> tuple[bytes, list[dict[str, Any]]]:
    sheet = _words(change.get("sheet"), "sheet name")
    cell = str(change.get("cell") or "").upper()
    if not _CELL.fullmatch(cell):
        raise Refused("Name the cell like B7.")
    value = change.get("value")
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise Refused("A cell's new value is a number or text.")
    with _open(data) as z:
        part = _sheet_part(z, sheet)
        xml = z.read(part).decode("utf-8")
        shared = []
        if "xl/sharedStrings.xml" in z.namelist():
            sst = z.read("xl/sharedStrings.xml").decode("utf-8")
            shared = ["".join(unescape(t) for t in re.findall(r"<t(?:\s[^>]*)?>(.*?)</t>", si, re.S))
                      for si in re.findall(r"<si>(.*?)</si>", sst, re.S)]
    found = list(re.finditer(rf'<c\s[^>]*\br="{cell}"[^>]*?(?:/>|>.*?</c>)', xml, re.S))
    if len(found) != 1:
        raise Refused(f"The sheet holds no cell {cell} to change (only existing cells are "
                      "edited).")
    old = found[0].group(0)
    if "<f>" in old or "<f " in old:
        raise Refused(f"{cell} holds a formula: change the cells it reads instead.")
    attrs = re.match(r"<c(\s[^>]*?)(/?)>", old).group(1)  # type: ignore[union-attr]
    kind = re.search(r'\bt="([^"]*)"', attrs)
    v = re.search(r"<v>(.*?)</v>", old, re.S)
    if kind and kind.group(1) == "s" and v:
        before: Any = shared[int(v.group(1))] if int(v.group(1)) < len(shared) else None
    elif kind and kind.group(1) == "inlineStr":
        before = "".join(unescape(t) for t in re.findall(r"<t(?:\s[^>]*)?>(.*?)</t>", old, re.S))
    else:
        before = unescape(v.group(1)) if v else None
    keep = re.sub(r'\s+t="[^"]*"', "", attrs)
    text = str(value)
    if isinstance(value, (int, float)) or _NUMBER.fullmatch(text):
        if not _NUMBER.fullmatch(text):
            raise Refused("That number cannot be written exactly.")
        new = f"<c{keep}><v>{text}</v></c>"
    else:
        text = _words(text, "cell text", empty=True)
        new = f'<c{keep} t="inlineStr"><is><t xml:space="preserve">' \
              f"{escape(text, quote=False)}</t></is></c>"
    xml_new = xml[:found[0].start()] + new + xml[found[0].end():]
    return _rezip(data, {part: xml_new.encode("utf-8")}), [
        {"field": f"{sheet}!{cell}", "before": before, "after": value}]
