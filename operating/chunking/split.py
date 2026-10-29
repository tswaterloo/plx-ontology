"""Structural splitters: a text becomes its units of meaning, never fixed windows.

* ``mail_own_text``: a message's own text, quoted reply/forward history removed with the
  one quoted-history grammar (``ontology.mentions.quoted_ranges``); a message that is
  only a forward keeps the forwarded message (its substance) and says so.
* ``sections``: a document's sections by markdown heading (DOCX/MarkItDown text), PPTX
  slide (``<!-- Slide number: N -->``) or PDF page (the caller passes pages); each
  section is packed to ``MAX_BODY`` at paragraph, then sentence, boundaries, and a pipe
  table is split row-wise with its header row repeated.
* ``sheet_blocks``: a workbook's rows (``integrations.xlsx_extract`` text: ``[Sheet: x]``
  then ``A1=v | B1=w`` rows) read against the sheet's first row as column headers, packed
  into blocks that each repeat the header.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ontology.mentions import quoted_ranges

from . import MAX_BODY

_SLIDE = re.compile(r"^<!-- Slide number: (\d+) -->\s*$")
_HEADING = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")
_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(])")
_SHEET = re.compile(r"^\[Sheet: (.*)\]$")
_CELL = re.compile(r"^([A-Z]{1,4})([1-9][0-9]{0,6})=(.*)$")
_INTERPRETED = re.compile(r"\[Sheet [^\]]*; cells [^\]]*\]$")
FORWARD_KEEP = 40   # own characters under which a forward's first quoted message is kept


@dataclass(frozen=True)
class Section:
    label: str | None   # heading path, "Slide 3", "Page 2", "Sheet 'x' rows 2-40"
    text: str


def mail_own_text(text: str) -> tuple[str, bool]:
    """(the sender's own text, whether it is a forwarded message kept instead)."""
    if not text:
        return "", False
    ranges = sorted(quoted_ranges(text))
    own, at = [], 0
    for start, end in ranges:
        if start > at:
            own.append(text[at:start])
        at = max(at, end)
    own.append(text[at:])
    mine = "".join(own).strip()
    if len(mine) >= FORWARD_KEEP or not ranges:
        return mine, False
    # Only a forward (or a one-line "see below"): the forwarded message is the substance,
    # without the older history quoted inside it.
    start, end = ranges[0]
    block = forwarded_message(text[start:end])
    return ((mine + "\n\n" if mine else "") + block).strip(), True


_FORWARD_HEADER = re.compile(r"^[ \t]*(?:From|Sent|Date|To|Cc|Bcc|Von|Gesendet|An|De|Envoy(?:é|e)|"
                             r"[ÀA]|Da|Inviato|A|Enviado|Para)[ \t]*:", re.IGNORECASE)
_SUBJECT = re.compile(r"^[ \t]*(?:Subject|Betreff|Objet|Oggetto|Asunto)[ \t]*:(.*)$",
                      re.IGNORECASE)


def forwarded_message(block: str) -> str:
    """The first message of a quoted/forwarded block: its divider and header lines dropped
    (the subject kept), '>' marks removed, and everything from the next history marker
    on (the older messages it quotes) cut."""
    lines = re.sub(r"(?m)^[ \t]*>[ \t]?", "", block).splitlines()
    index, subject = 1 if lines else 0, None   # the divider / attribution line
    while index < len(lines):
        line = lines[index]
        found = _SUBJECT.match(line)
        if found:
            subject = found.group(1).strip()
        elif not (_FORWARD_HEADER.match(line) or not line.strip()):
            break
        index += 1
    rest = "\n".join(lines[index:])
    inner = sorted(quoted_ranges(rest))
    if inner:
        rest = rest[:inner[0][0]]
    rest = rest.strip()
    return (f"Subject: {subject}\n\n{rest}" if subject else rest).strip()


def pack(text: str, limit: int = MAX_BODY) -> list[str]:
    """``text`` cut into pieces of at most ``limit`` characters at paragraph, then line,
    then sentence, then word boundaries (a pipe table row-wise, header repeated)."""
    text = text.strip()
    if len(text) <= limit:
        return [text] if text else []
    out: list[str] = []
    current = ""
    for block in _blocks(text, limit):
        if current and len(current) + 2 + len(block) > limit:
            out.append(current)
            current = ""
        current = f"{current}\n\n{block}" if current else block
    if current:
        out.append(current)
    return out


def _blocks(text: str, limit: int) -> list[str]:
    out: list[str] = []
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        if not para:
            continue
        if len(para) <= limit:
            out.append(para)
        elif _is_table(para):
            out.extend(_table_pieces(para, limit))
        else:
            out.extend(_long_pieces(para, limit))
    return out


def _is_table(para: str) -> bool:
    lines = para.splitlines()
    return len(lines) >= 3 and all(line.lstrip().startswith("|") for line in lines[:3])


def _table_pieces(para: str, limit: int) -> list[str]:
    lines = para.splitlines()
    head = "\n".join(lines[:2])
    out, current = [], head
    for row in lines[2:]:
        if len(current) + 1 + len(row) > limit and current != head:
            out.append(current)
            current = head
        current += "\n" + row[: max(0, limit - len(head) - 1)]
    if current != head:
        out.append(current)
    return out


def _long_pieces(para: str, limit: int) -> list[str]:
    units: list[str] = []
    for line in para.splitlines():
        units.extend(_SENTENCE.split(line) if len(line) > limit else [line])
    out, current = [], ""
    for unit in units:
        while len(unit) > limit:     # one unbroken run: cut at the last space
            cut = unit.rfind(" ", limit // 2, limit)
            cut = cut if cut > 0 else limit
            if current:
                out.append(current)
                current = ""
            out.append(unit[:cut].strip())
            unit = unit[cut:].strip()
        if current and len(current) + 1 + len(unit) > limit:
            out.append(current)
            current = ""
        current = f"{current}\n{unit}" if current else unit
    if current:
        out.append(current)
    return [piece for piece in out if piece.strip()]


def sections(text: str) -> list[Section]:
    """A document's sections by heading or slide (module docstring); text with neither
    is one section."""
    out: list[Section] = []
    path: list[tuple[int, str]] = []
    label: str | None = None
    lines: list[str] = []

    def close() -> None:
        body = "\n".join(lines).strip()
        if body:
            out.append(Section(label, body))
        lines.clear()

    for line in text.splitlines():
        slide = _SLIDE.match(line)
        heading = _HEADING.match(line)
        if slide:
            close()
            path = []
            label = f"Slide {slide.group(1)}"
            continue
        if heading:
            close()
            level = len(heading.group(1))
            path = [(lv, name) for lv, name in path if lv < level] + [(level, heading.group(2))]
            base = label.split(" > ")[0] if label and label.startswith("Slide ") else None
            label = " > ".join(([base] if base else []) + [name for _, name in path])
            lines.append(line)
            continue
        lines.append(line)
    close()
    return _merge_short(out)


def _merge_short(found: list[Section], short: int = 60) -> list[Section]:
    """A section too short to stand alone (a title and a few words) joins the next one, so
    no heading or intro is lost as a fragment."""
    out: list[Section] = []
    carry: Section | None = None
    for index, section in enumerate(found):
        if carry is not None:
            section = Section(section.label, carry.text + "\n\n" + section.text)
            carry = None
        if len(section.text) < short and index < len(found) - 1:
            carry = section
            continue
        out.append(section)
    if carry is not None:
        out.append(carry)
    return out


def sheet_blocks(text: str, limit: int = MAX_BODY) -> list[Section]:
    """A workbook's rows, row-wise, each block repeating its sheet's column headers."""
    out: list[Section] = []
    sheet, header, rows = None, {}, []

    def flush() -> None:
        if sheet is None or not rows:
            return
        columns = " | ".join(header[c] for c in sorted(header, key=_col)) if header else ""
        head = f"Columns: {columns}"[:400] if columns else ""   # a very wide sheet: cut
        block, first, last = [], None, None
        for number, line in rows:
            if block and len(head) + sum(len(b) + 1 for b in block) + len(line) > limit:
                out.append(Section(f"Sheet '{sheet}' rows {first}-{last}",
                                   "\n".join(([head] if head else []) + block)))
                block, first = [], None
            block.append(line[: max(0, limit - len(head) - 2)])
            first = number if first is None else first
            last = number
        if block:
            out.append(Section(f"Sheet '{sheet}' rows {first}-{last}",
                               "\n".join(([head] if head else []) + block)))

    for raw in text.splitlines():
        found = _SHEET.match(raw.strip())
        if found:
            flush()
            sheet, header, rows = found.group(1), {}, []
            continue
        if sheet is None or _INTERPRETED.search(raw):
            continue
        cells = []
        number = None
        for piece in raw.split(" | "):
            cell = _CELL.match(piece.strip())
            if cell is None:
                cells = []
                break
            number = int(cell.group(2))
            value = cell.group(3)
            if value.startswith("FORMULA(") and "; cached=" in value:
                value = value.rsplit("; cached=", 1)[1]
            cells.append((cell.group(1), value.strip()))
        if not cells:
            continue
        if not header:
            header = {col: value for col, value in cells if value}
            continue
        line = "; ".join(f"{header.get(col, col)}: {value}" for col, value in cells if value)
        if line:
            rows.append((number, line))
    flush()
    return out


def _col(letters: str) -> int:
    n = 0
    for ch in letters:
        n = n * 26 + ord(ch) - 64
    return n


def is_workbook_text(text: str) -> bool:
    return text.lstrip().startswith("[Sheet: ")


__all__ = ["Section", "is_workbook_text", "mail_own_text", "pack", "sections", "sheet_blocks"]
