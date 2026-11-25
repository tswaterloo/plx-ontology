"""How a made document looks: the Markdown the model writes read into blocks once, and those
blocks laid out as a person would format the file by hand (``instruct_files.make`` calls
these; nothing here reads a source or writes anywhere).

* **Blocks** (``blocks``): ``#``/``##``/``###`` headings, paragraphs, ``-``/``*`` bullets,
  ``|`` tables (a rule row ``|---|`` under the header, or a line that starts with ``|``),
  a ``---`` line a divider, and the charts placed by ``[chart N]`` lines. Inline:
  ``**bold**``, ``*italic*``, ``~~struck~~`` and the CriticMarkup marks a redline uses,
  ``{++inserted++}`` and ``{--deleted--}``.
* **Numbers in tables** (``column_kinds``): a column whose cells are all numbers is right
  aligned, and when any of them has decimals every cell shows the same decimals with
  thousands separators (21.8 beside 9417.60 reads 21.80 and 9,417.60). Whole-number
  columns keep their digits as written (a PO number or a year never gets a comma).
* **Word** (``docx_parts``): a styles part (Calibri, real Heading 1-3 styles so Word's
  navigation and table of contents read them, a table style), letter pages with one inch
  margins, header rows shaded and repeated on each page, numbers right aligned; inserted
  and deleted marks are Word tracked changes the reader accepts or rejects.
* **Excel** (``xlsx_sheet``): numbers, money (``$1,234.56``, ``(2,500.00)``), percents and
  ISO dates written as real numbers and dates with their formats; the header row bold on a
  fill, frozen and filtered; column widths from the content; a ``**bold**`` cell bold.
* **PDF** (``pdf``, optional ``reportlab``): flowing paragraphs with word wrap, real tables
  (wrapped cells, shaded header repeated on each page, numbers right aligned), bullets,
  charts, and page numbers.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal, InvalidOperation
import io
import re
from typing import Any, Mapping, Optional, Sequence
from xml.sax.saxutils import escape

CHART_AT = re.compile(r"^\s*\[chart\s+([1-9][0-9]?)\]\s*$", re.I)
TABLE_CITE_AT = re.compile(r"^\s*\[table-cite\s+([0-9]+)\]\s*$")
HEADING = re.compile(r"^(#{1,3})\s+(.*)$")
BULLET = re.compile(r"^(\s*)[-*\u2022]\s+(.*)$")
RULE_ROW = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
DIVIDER = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$")
_INLINE = re.compile(r"(\{\+\+.*?\+\+\}|\{--.*?--\}|~~.+?~~|\*\*.+?\*\*|(?<![\w*])\*(?!\s)[^*]+?\*(?![\w*]))",
                     re.S)


# ------------------------------------------------------------------------------ blocks

def cells(line: str) -> list[str]:
    row = line.strip()
    if row.startswith("|"):
        row = row[1:]
    if row.endswith("|") and not row.endswith("\\|"):
        row = row[:-1]
    return [c.strip() for c in row.split("|")]


def table_lines(lines: Sequence[str]) -> set[int]:
    """Which lines are rows of a Markdown table: a line starting with "|", or a run of
    lines with a "|" between cells whose header is followed by its rule row ("--- | ---")."""
    out = {i for i, line in enumerate(lines) if line.lstrip().startswith("|")}
    for i in range(len(lines) - 1):
        if "|" in lines[i] and RULE_ROW.match(lines[i + 1].strip()) and "|" in lines[i + 1]:
            j = i
            while j < len(lines) and "|" in lines[j] and lines[j].strip():
                out.add(j)
                j += 1
    return out


def blocks(content: str, charts: Sequence[Mapping[str, Any]] = ()) -> list[tuple]:
    """The content as blocks in order: ("heading", level, text), ("para", text),
    ("bullet", level, text), ("table", rows, has_header), ("divider",), ("chart", chart).
    A "[chart N]" line places chart N (a line naming no chart is dropped); the charts no
    line names follow at the end. Blank lines only separate blocks."""
    lines = content.split("\n")
    rows_at = table_lines(lines)
    out: list[tuple] = []
    used: set[int] = set()
    table: list[list[str]] = []
    header = False

    def flush() -> None:
        nonlocal table, header
        if table:
            out.append(("table", table, header))
        table, header = [], False

    for i, line in enumerate(lines):
        if i in rows_at:
            if RULE_ROW.match(line.strip()):
                header = header or len(table) == 1
            else:
                table.append(cells(line))
            continue
        flush()
        at = CHART_AT.match(line)
        if at:
            n = int(at.group(1)) - 1
            if n < len(charts) and n not in used:
                used.add(n)
                out.append(("chart", charts[n]))
            continue
        ct = TABLE_CITE_AT.match(line)
        if ct:
            out.append(("cite", int(ct.group(1))))
            continue
        if not line.strip():
            continue
        heading, bullet = HEADING.match(line.strip()), BULLET.match(line)
        if heading:
            out.append(("heading", len(heading.group(1)), heading.group(2).strip()))
        elif DIVIDER.match(line):
            out.append(("divider",))
        elif bullet:
            out.append(("bullet", min(len(bullet.group(1).expandtabs(4)) // 2, 2),
                        bullet.group(2)))
        else:
            out.append(("para", line.strip()))
    flush()
    out += [("chart", c) for n, c in enumerate(charts) if n not in used]
    return out


def runs(text: str) -> list[tuple[str, frozenset[str]]]:
    """``text`` as (text, marks) runs: b, i, strike, ins, del."""
    out: list[tuple[str, frozenset[str]]] = []
    for part in _INLINE.split(text):
        if not part:
            continue
        if part.startswith("{++") and part.endswith("++}"):
            out += [(t, m | {"ins"}) for t, m in runs(part[3:-3])]
        elif part.startswith("{--") and part.endswith("--}"):
            out += [(t, m | {"del"}) for t, m in runs(part[3:-3])]
        elif part.startswith("~~") and part.endswith("~~") and len(part) > 4:
            out += [(t, m | {"strike"}) for t, m in runs(part[2:-2])]
        elif part.startswith("**") and part.endswith("**") and len(part) > 4:
            out += [(t, m | {"b"}) for t, m in runs(part[2:-2])]
        elif part.startswith("*") and part.endswith("*") and len(part) > 2:
            out += [(t, m | {"i"}) for t, m in runs(part[1:-1])]
        else:
            out.append((part, frozenset()))
    return out


def plain(text: str) -> str:
    return "".join(t for t, m in runs(text) if "del" not in m)


# the chat answer's mark of a figure it could not trace to a record (``ask_refs``: none since A2): it
# belongs in the answer, never in a file made or edited for the person
_DOUBT = re.compile(r"[ \t]*\((?:unverified|not verified)\)", re.I)


def without_doubt_marks(text: str) -> str:
    return _DOUBT.sub("", text)


# ------------------------------------------------------------------------------ numbers

_PLAIN_NUMBER = re.compile(r"^-?\d+(?:\.\d+)?$")
_MONEY = re.compile(r"^(?P<neg>-|\()?\s*(?P<cur>[$\u20ac\u00a3])?\s*(?P<num>\d{1,3}(?:,\d{3})+"
                    r"(?:\.\d+)?|\d+(?:\.\d+)?)\s*\)?$")
_PERCENT = re.compile(r"^(-?\d+(?:\.\d+)?)\s*%$")
_ISO_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


def column_kinds(rows: Sequence[Sequence[str]], has_header: bool) -> list[Optional[int]]:
    """Per column: the decimals its numbers show (0 for whole numbers), or None when it is
    not a column of plain numbers (any cell text, a money sign or a date)."""
    body = rows[1:] if has_header else rows
    width = max((len(r) for r in rows), default=0)
    out: list[Optional[int]] = []
    for c in range(width):
        values = [plain(r[c]).strip() for r in body if c < len(r) and plain(r[c]).strip()]
        if not values or not all(_PLAIN_NUMBER.match(v) for v in values) \
                or any(len(v.lstrip("-").split(".")[0]) > 1 and v.lstrip("-").startswith("0")
                       for v in values):
            out.append(None)
            continue
        places = min(max(len(v.split(".")[1]) if "." in v else 0 for v in values), 4)
        out.append(2 if places == 1 else places)   # 21.8 beside 9417.60 reads 21.80
    return out


def shown(value: str, decimals: Optional[int]) -> str:
    """A plain number as its column shows it (``column_kinds``)."""
    if not decimals or not _PLAIN_NUMBER.match(value.strip()):
        return value
    try:
        return f"{Decimal(value.strip()):,.{decimals}f}"
    except InvalidOperation:
        return value


def cell_value(text: str) -> tuple[str, Any, str]:
    """An Excel cell from its text: ("n", number, format) for a number, money or percent,
    ("d", serial, "date") for an ISO date, else ("s", text, "")."""
    raw = text.strip()
    if _PLAIN_NUMBER.match(raw) and not (len(raw.lstrip("-").split(".")[0]) > 1
                                         and raw.lstrip("-").startswith("0")):
        if len(raw.replace("-", "").replace(".", "")) > 15:
            return "s", text, ""
        decimals = len(raw.split(".")[1]) if "." in raw else 0
        return "n", raw, f"dec{min(decimals, 4)}" if decimals else ""
    m = _MONEY.match(raw)
    if m and (m.group("cur") or "," in m.group("num") or m.group("neg") == "("):
        if (m.group("neg") == "(") != raw.endswith(")"):
            return "s", text, ""
        num = m.group("num").replace(",", "")
        decimals = len(num.split(".")[1]) if "." in num else 0
        value = ("-" if m.group("neg") else "") + num
        if m.group("cur") == "$":
            return "n", value, "money"
        if m.group("cur"):
            return "s", text, ""
        return "n", value, f"dec{min(decimals, 4)}" if decimals else "int"
    m = _PERCENT.match(raw)
    if m:
        return "n", str(Decimal(m.group(1)) / 100), "pct"
    m = _ISO_DATE.match(raw)
    if m:
        try:
            day = dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return "s", text, ""
        return "d", str((day - dt.date(1899, 12, 30)).days), "date"
    return "s", text, ""


# ------------------------------------------------------------------------------ Word

W_NS = ('xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"')
ACCENT, HEAD_FILL, RULE = "1F3864", "D9E2F3", "8EAADB"


def _style(sid: str, name: str, ppr: str = "", rpr: str = "", kind: str = "paragraph",
           based: str = "Normal", extra: str = "") -> str:
    return (f'<w:style w:type="{kind}" w:styleId="{sid}"><w:name w:val="{name}"/>'
            + (f'<w:basedOn w:val="{based}"/>' if based else "")
            + f'<w:qFormat/>{extra}<w:pPr>{ppr}</w:pPr><w:rPr>{rpr}</w:rPr></w:style>')


DOCX_STYLES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    f'<w:styles {W_NS}><w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:ascii="Calibri" '
    'w:hAnsi="Calibri" w:eastAsia="Calibri" w:cs="Calibri"/><w:sz w:val="22"/><w:szCs '
    'w:val="22"/><w:lang w:val="en-CA"/></w:rPr></w:rPrDefault><w:pPrDefault><w:pPr>'
    '<w:spacing w:after="120" w:line="264" w:lineRule="auto"/></w:pPr></w:pPrDefault>'
    "</w:docDefaults>"
    + _style("Normal", "Normal", based="")
    + _style("Title", "Title", '<w:spacing w:after="200"/>',
             f'<w:b/><w:color w:val="{ACCENT}"/><w:sz w:val="40"/>')
    + _style("Heading1", "heading 1", '<w:keepNext/><w:spacing w:before="320" w:after="120"/>'
             '<w:outlineLvl w:val="0"/>', f'<w:b/><w:color w:val="{ACCENT}"/><w:sz w:val="32"/>')
    + _style("Heading2", "heading 2", '<w:keepNext/><w:spacing w:before="240" w:after="80"/>'
             '<w:outlineLvl w:val="1"/>', f'<w:b/><w:color w:val="{ACCENT}"/><w:sz w:val="26"/>')
    + _style("Heading3", "heading 3", '<w:keepNext/><w:spacing w:before="200" w:after="60"/>'
             '<w:outlineLvl w:val="2"/>', '<w:b/><w:sz w:val="23"/>')
    + _style("ListBullet", "List Bullet", '<w:spacing w:after="60"/>')
    + _style("TableText", "Table Text", '<w:spacing w:before="20" w:after="20" w:line="240" '
             'w:lineRule="auto"/>', '<w:sz w:val="20"/>')
    + '<w:style w:type="table" w:styleId="PlainTable"><w:name w:val="Plain Table"/>'
      '<w:tblPr><w:tblBorders>'
    + "".join(f'<w:{s} w:val="single" w:sz="4" w:space="0" w:color="{RULE}"/>'
              for s in ("top", "left", "bottom", "right", "insideH", "insideV"))
    + '</w:tblBorders><w:tblCellMar><w:left w:w="80" w:type="dxa"/><w:right w:w="80" '
      'w:type="dxa"/></w:tblCellMar></w:tblPr></w:style></w:styles>')

# a Word bullet list (numId 1), three levels
DOCX_NUMBERING = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    f'<w:numbering {W_NS}><w:abstractNum w:abstractNumId="0"><w:multiLevelType '
    'w:val="hybridMultilevel"/>'
    + "".join(f'<w:lvl w:ilvl="{n}"><w:start w:val="1"/><w:numFmt w:val="bullet"/><w:lvlText '
              f'w:val="{ch}"/><w:lvlJc w:val="left"/><w:pPr><w:ind w:left="{360 + 360 * n}" '
              'w:hanging="270"/></w:pPr><w:rPr><w:rFonts w:ascii="Calibri" w:hAnsi="Calibri"/>'
              "</w:rPr></w:lvl>" for n, ch in enumerate(("\u2022", "\u2013", "\u2022")))
    + '</w:abstractNum><w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num></w:numbering>')

# letter, one inch margins
DOCX_SECTION = ('<w:sectPr><w:pgSz w:w="12240" w:h="15840"/><w:pgMar w:top="1440" '
                'w:right="1440" w:bottom="1440" w:left="1440" w:header="720" w:footer="720" '
                'w:gutter="0"/></w:sectPr>')
TEXT_WIDTH = 9360   # twentieths of a point between the margins


class _Ids:
    def __init__(self) -> None:
        self.n = 0

    def next(self) -> int:
        self.n += 1
        return self.n


def docx_runs(text: str, ids: _Ids, *, bold: bool = False, size: int = 0) -> str:
    out = []
    for part, marks in runs(text):
        props = ("<w:b/>" if bold or "b" in marks else "") + ("<w:i/>" if "i" in marks else "") \
            + ("<w:strike/>" if "strike" in marks else "") \
            + (f'<w:sz w:val="{size}"/>' if size else "")
        rpr = f"<w:rPr>{props}</w:rPr>" if props else ""
        if "del" in marks:
            out.append(f'<w:del w:id="{ids.next()}" w:author="Workbench"><w:r>{rpr}'
                       f'<w:delText xml:space="preserve">{escape(part)}</w:delText></w:r></w:del>')
            continue
        run = f'<w:r>{rpr}<w:t xml:space="preserve">{escape(part)}</w:t></w:r>'
        out.append(f'<w:ins w:id="{ids.next()}" w:author="Workbench">{run}</w:ins>'
                   if "ins" in marks else run)
    return "".join(out)


def _width(text: str) -> int:
    return max((len(w) for w in plain(text).split()), default=1) + len(plain(text)) // 3


def docx_table(rows: list[list[str]], has_header: bool, ids: _Ids) -> str:
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    header = has_header or (len(rows) > 1 and all(plain(c).strip() and column_kinds(
        [[c]], False)[0] is None for c in rows[0]))
    kinds = column_kinds(rows, header)
    weights = [max(3, min(40, max(_width(r[c]) for r in rows))) for c in range(width)]
    grid = [max(500, TEXT_WIDTH * w // sum(weights)) for w in weights]
    out = [f'<w:tbl><w:tblPr><w:tblStyle w:val="PlainTable"/><w:tblW w:w="{sum(grid)}" '
           'w:type="dxa"/><w:tblLayout w:type="fixed"/></w:tblPr><w:tblGrid>'
           + "".join(f'<w:gridCol w:w="{g}"/>' for g in grid) + "</w:tblGrid>"]
    for i, row in enumerate(rows):
        head = header and i == 0
        trpr = "<w:trPr><w:tblHeader/><w:cantSplit/></w:trPr>" if head else \
            "<w:trPr><w:cantSplit/></w:trPr>"
        tcs = []
        for c, value in enumerate(row):
            right = kinds[c] is not None
            text = value if head else shown(value, kinds[c])
            shade = f'<w:shd w:val="clear" w:color="auto" w:fill="{HEAD_FILL}"/>' if head else ""
            tcs.append(f'<w:tc><w:tcPr><w:tcW w:w="{grid[c]}" w:type="dxa"/>{shade}</w:tcPr>'
                       f'<w:p><w:pPr><w:pStyle w:val="TableText"/>'
                       f'{"<w:jc w:val=\"right\"/>" if right else ""}</w:pPr>'
                       f"{docx_runs(text, ids, bold=head)}</w:p></w:tc>")
        out.append(f"<w:tr>{trpr}{''.join(tcs)}</w:tr>")
    return "".join(out) + "</w:tbl><w:p/>"


def docx_block(block: tuple, ids: _Ids) -> str:
    kind = block[0]
    if kind == "heading":
        return (f'<w:p><w:pPr><w:pStyle w:val="Heading{block[1]}"/></w:pPr>'
                f"{docx_runs(block[2], ids)}</w:p>")
    if kind == "bullet":
        return (f'<w:p><w:pPr><w:pStyle w:val="ListBullet"/><w:numPr><w:ilvl '
                f'w:val="{block[1]}"/><w:numId w:val="1"/></w:numPr></w:pPr>'
                f"{docx_runs(block[2], ids)}</w:p>")
    if kind == "divider":
        return (f'<w:p><w:pPr><w:pBdr><w:bottom w:val="single" w:sz="6" w:space="1" '
                f'w:color="{RULE}"/></w:pBdr></w:pPr></w:p>')
    if kind == "table":
        return docx_table(block[1], block[2], ids)
    return f"<w:p>{docx_runs(block[1], ids)}</w:p>"


# ------------------------------------------------------------------------------ Excel

# the chart workbook's five cell formats (``chart_ooxml.STYLES``: 0 plain, 1 bold, 2 note,
# 3 #,##0.00, 4 #,##0) first, so a sheet of text and the chart sheets share one styles part
XLSX_FORMATS = {"": 0, "int": 4, "dec2": 3, "head": 5, "date": 6, "money": 7, "pct": 8,
                "title": 9, "dec1": 10, "dec3": 11, "dec4": 12, "bold": 1,
                "bold dec2": 13, "bold money": 14, "bold int": 15}
XLSX_STYLES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
    '<numFmts count="6"><numFmt numFmtId="164" formatCode="#,##0.00"/><numFmt numFmtId="165" '
    'formatCode="yyyy-mm-dd"/><numFmt numFmtId="166" formatCode="&quot;$&quot;#,##0.00;'
    '\\-&quot;$&quot;#,##0.00"/><numFmt numFmtId="167" formatCode="0.0%"/><numFmt '
    'numFmtId="168" formatCode="#,##0.0"/><numFmt numFmtId="169" formatCode="#,##0.000"/>'
    '</numFmts><fonts count="4"><font><sz val="11"/><name val="Calibri"/></font><font><b/>'
    '<sz val="11"/><name val="Calibri"/></font><font><i/><sz val="9"/><color rgb="FF6B7280"/>'
    '<name val="Calibri"/></font><font><b/><sz val="13"/><name val="Calibri"/></font></fonts>'
    '<fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill '
    'patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor '
    f'rgb="FF{HEAD_FILL}"/><bgColor indexed="64"/></patternFill></fill></fills>'
    '<borders count="2"><border><left/><right/><top/><bottom/><diagonal/></border><border>'
    f'<left/><right/><top/><bottom style="thin"><color rgb="FF{RULE}"/></bottom><diagonal/>'
    '</border></borders><cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" '
    'borderId="0"/></cellStyleXfs><cellXfs count="16">'
    '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
    '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/>'
    '<xf numFmtId="0" fontId="2" fillId="0" borderId="0" xfId="0" applyFont="1" '
    'applyAlignment="1"><alignment wrapText="1" vertical="top"/></xf>'
    '<xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
    '<xf numFmtId="3" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
    '<xf numFmtId="0" fontId="1" fillId="2" borderId="1" xfId="0" applyFont="1" applyFill="1" '
    'applyBorder="1" applyAlignment="1"><alignment vertical="center" wrapText="1"/></xf>'
    '<xf numFmtId="165" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1" '
    'applyAlignment="1"><alignment horizontal="left"/></xf>'
    '<xf numFmtId="166" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
    '<xf numFmtId="167" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
    '<xf numFmtId="0" fontId="3" fillId="0" borderId="0" xfId="0" applyFont="1"/>'
    '<xf numFmtId="168" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
    '<xf numFmtId="169" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
    '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
    '<xf numFmtId="164" fontId="1" fillId="0" borderId="0" xfId="0" applyNumberFormat="1" '
    'applyFont="1"/><xf numFmtId="166" fontId="1" fillId="0" borderId="0" xfId="0" '
    'applyNumberFormat="1" applyFont="1"/><xf numFmtId="3" fontId="1" fillId="0" borderId="0" '
    'xfId="0" applyNumberFormat="1" applyFont="1"/>'
    '</cellXfs></styleSheet>')


def column(n: int) -> str:
    out = ""
    while True:
        n, r = divmod(n, 26)
        out = chr(65 + r) + out
        if n == 0:
            return out
        n -= 1


def xlsx_sheet(rows: Sequence[Sequence[str]], max_cols: Optional[int] = None, *,
               literal: bool = False) -> str:
    """A worksheet of ``rows`` (each a list of cell texts) with typed cells, a styled and
    frozen header row and column widths from the content (module doc, **Excel**).
    ``literal`` keeps cell text verbatim without Markdown inline parsing."""
    def text_of(raw: str) -> str:
        return raw if literal else plain(raw)

    rows = [list(r)[:max_cols] if max_cols is not None else list(r) for r in rows]
    header_at = None
    for i, r in enumerate(rows[:6]):
        texts = [text_of(c).strip() for c in r]
        if len([t for t in texts if t]) >= 2 and all(cell_value(t)[0] == "s" for t in texts if t) \
                and i + 1 < len(rows) and len(rows[i + 1]) >= 2:
            header_at = i
            break
    decimals: dict[int, int] = {}
    for r, row in enumerate(rows):
        for c, raw in enumerate(row):
            kind, _v, fmt = cell_value(text_of(raw))
            if kind == "n" and fmt.startswith("dec") and r != header_at:
                decimals[c] = max(decimals.get(c, 0), int(fmt[3:]), 2)
    widths: dict[int, int] = {}
    xml_rows = []
    for r, row in enumerate(rows, 1):
        xml = []
        single = len([c for c in row if c.strip()]) == 1 and len(row) == 1
        for c, raw in enumerate(row):
            text = text_of(raw)
            ref = f"{column(c)}{r}"
            if not text.strip():   # a blank the line holds (a form's "Label:|") stays a cell
                xml.append(f'<c r="{ref}" t="inlineStr"><is><t></t></is></c>')
                continue
            bold = False if literal else (
                any("b" in m for _, m in runs(raw))
                and all("b" in m for t, m in runs(raw) if t.strip())
            )
            kind, value, fmt = cell_value(text)
            if kind == "n" and fmt in ("", "int") or fmt.startswith("dec"):
                fmt = f"dec{decimals[c]}" if decimals.get(c) else fmt
            if header_at is not None and r - 1 == header_at:
                kind, value, fmt = "s", text, "head"
            elif kind == "s" and single and header_at is not None and r - 1 < header_at:
                fmt = "title"
            elif bold:
                fmt = "bold" if kind == "s" or not fmt else f"bold {fmt}" \
                    if f"bold {fmt}" in XLSX_FORMATS else fmt
            style = XLSX_FORMATS.get(fmt, 0)
            s = f' s="{style}"' if style else ""
            if kind in ("n", "d"):
                xml.append(f'<c r="{ref}"{s}><v>{value}</v></c>')
                shown_len = len(text.strip()) + 2
            else:
                xml.append(f'<c r="{ref}"{s} t="inlineStr"><is><t xml:space="preserve">'
                           f"{escape(text.strip())}</t></is></c>")
                shown_len = len(text.strip())
            if not (single and fmt == "title"):
                widths[c] = max(widths.get(c, 0), shown_len)
        xml_rows.append(f'<row r="{r}">' + "".join(xml) + "</row>")
    cols = "".join(f'<col min="{c + 1}" max="{c + 1}" width="{min(max(w + 2, 8), 60)}" '
                   'customWidth="1"/>' for c, w in sorted(widths.items()))
    view = ""
    filt = ""
    if header_at is not None:
        top = f"A{header_at + 2}"
        view = (f'<sheetViews><sheetView workbookViewId="0"><pane ySplit="{header_at + 1}" '
                f'topLeftCell="{top}" activePane="bottomLeft" state="frozen"/></sheetView>'
                "</sheetViews>")
        last = header_at + 1
        while last < len(rows) and any(c.strip() for c in rows[last]):
            last += 1
        width = len(rows[header_at])
        if last > header_at + 1:
            filt = f'<autoFilter ref="A{header_at + 1}:{column(width - 1)}{last}"/>'
    ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    return (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><worksheet {ns}>{view}'
            + (f"<cols>{cols}</cols>" if cols else "") + "<sheetData>" + "".join(xml_rows)
            + f"</sheetData>{filt}</worksheet>")


# ------------------------------------------------------------------------------ PDF

def _pdf_markup(text: str) -> str:
    out = []
    for part, marks in runs(text):
        t = escape(part)
        if "b" in marks:
            t = f"<b>{t}</b>"
        if "i" in marks:
            t = f"<i>{t}</i>"
        if "strike" in marks:
            t = f"<strike>{t}</strike>"
        if "del" in marks:
            t = f'<font color="#B91C1C"><strike>{t}</strike></font>'
        if "ins" in marks:
            t = f'<font color="#1D4ED8"><u>{t}</u></font>'
        out.append(t)
    return "".join(out)


def pdf(content: str, charts: Sequence[Mapping[str, Any]] = (), title: str = "",
        table_notes: Mapping[int, Sequence[str]] = (),
        citations: Sequence[Sequence[str]] = ()) -> bytes:
    """The content as a laid-out PDF (module doc, **PDF**). ImportError without
    reportlab (the caller refuses the kind)."""
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_RIGHT
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.platypus import (Flowable, HRFlowable, KeepTogether, Paragraph,
                                    SimpleDocTemplate, Spacer, Table, TableStyle)
    from operating import chart_pdf

    ink, accent = colors.HexColor("#1F2937"), colors.HexColor("#" + ACCENT)
    body = ParagraphStyle("body", fontName="Helvetica", fontSize=10, leading=13.5,
                          textColor=ink, spaceAfter=5)
    heads = {1: ParagraphStyle("h1", parent=body, fontName="Helvetica-Bold", fontSize=16,
                               leading=20, textColor=accent, spaceBefore=10, spaceAfter=8),
             2: ParagraphStyle("h2", parent=body, fontName="Helvetica-Bold", fontSize=12.5,
                               leading=16, textColor=accent, spaceBefore=10, spaceAfter=5),
             3: ParagraphStyle("h3", parent=body, fontName="Helvetica-Bold", fontSize=11,
                               leading=14, spaceBefore=8, spaceAfter=4)}
    bullet = ParagraphStyle("bullet", parent=body, leftIndent=16, bulletIndent=4,
                            spaceAfter=3)
    width = letter[0] - 1.5 * inch

    class ChartFlowable(Flowable):
        def __init__(self, chart: Mapping[str, Any]) -> None:
            super().__init__()
            self.chart = chart
            self.h = chart_pdf.HEIGHT + 40

        def wrap(self, *_: Any) -> tuple[float, float]:
            return width, self.h

        def draw(self) -> None:
            chart_pdf.draw(self.canv, self.chart, 0, self.h, width, with_notes=False)

    note = ParagraphStyle("note", parent=body, fontSize=chart_pdf.NOTE_SIZE,
                          leading=chart_pdf.NOTE_LEAD, textColor=colors.HexColor("#6B7280"),
                          spaceAfter=0)

    def chart_notes(chart: Mapping[str, Any]) -> list[Any]:
        # the statement is the evidence of what the chart reads: never cut, it flows on
        from operating import chart_ooxml as ox
        return [Paragraph(escape(line), note) for line in ox.provenance(chart)]

    def table(rows: list[list[str]], has_header: bool) -> Table:
        n = max(len(r) for r in rows)
        rows = [r + [""] * (n - len(r)) for r in rows]
        header = has_header or (len(rows) > 1 and all(
            plain(c).strip() and column_kinds([[c]], False)[0] is None for c in rows[0]))
        kinds = column_kinds(rows, header)
        size = 9 if n <= 6 else 8 if n <= 9 else 7
        cell = ParagraphStyle("cell", parent=body, fontSize=size, leading=size + 2.5,
                              spaceAfter=0)
        num = ParagraphStyle("num", parent=cell, alignment=TA_RIGHT)
        head = ParagraphStyle("head", parent=cell, fontName="Helvetica-Bold")
        headnum = ParagraphStyle("headnum", parent=head, alignment=TA_RIGHT)
        data = []
        for i, r in enumerate(rows):
            line = []
            for c, value in enumerate(r):
                is_head = header and i == 0
                text = value if is_head else shown(value, kinds[c])
                style = (headnum if kinds[c] is not None else head) if is_head else \
                    (num if kinds[c] is not None else cell)
                line.append(Paragraph(_pdf_markup(text), style))
            data.append(line)
        weights = [max(4, min(40, max(_width(r[c]) for r in rows))) for c in range(n)]
        widths = [width * w / sum(weights) for w in weights]
        t = Table(data, colWidths=widths, repeatRows=1 if header else 0)
        style = [("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#" + RULE)),
                 ("VALIGN", (0, 0), (-1, -1), "TOP"),
                 ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]
        if header:
            style.append(("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#" + HEAD_FILL)))
        t.setStyle(TableStyle(style))
        return t

    story: list[Any] = []
    for block in blocks(content, charts):
        kind = block[0]
        if kind == "heading":
            story.append(Paragraph(_pdf_markup(block[2]), heads[block[1]]))
        elif kind == "para":
            story.append(Paragraph(_pdf_markup(block[1]), body))
        elif kind == "bullet":
            story.append(Paragraph(_pdf_markup(block[2]), bullet, bulletText="\u2022"))
        elif kind == "divider":
            story.append(HRFlowable(width="100%", thickness=0.6,
                                    color=colors.HexColor("#" + RULE), spaceBefore=4,
                                    spaceAfter=6))
        elif kind == "table":
            story += [table(block[1], block[2]), Spacer(1, 8)]
        elif kind == "cite":
            lines = table_notes.get(block[1]) if isinstance(table_notes, Mapping) else ()
            if lines:
                story += [Paragraph(escape(ln), note) for ln in lines] + [Spacer(1, 6)]
        elif kind == "chart":
            notes = chart_notes(block[1])
            story.append(KeepTogether([ChartFlowable(block[1])] + notes[:2]))
            story += notes[2:] + [Spacer(1, 10)]
    if citations:
        story.append(Paragraph("Sources and statements", heads[2]))
        for entry in citations:
            story += [Paragraph(escape(ln), note) for ln in entry] + [Spacer(1, 4)]

    def footer(canvas: Any, doc: Any) -> None:
        canvas.saveState()
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(colors.HexColor("#6B7280"))
        canvas.drawRightString(letter[0] - 0.75 * inch, 0.5 * inch, f"Page {doc.page}")
        canvas.restoreState()

    out = io.BytesIO()
    doc = SimpleDocTemplate(out, pagesize=letter, leftMargin=0.75 * inch,
                            rightMargin=0.75 * inch, topMargin=0.75 * inch,
                            bottomMargin=0.8 * inch, title=title, author="", creator="",
                            producer="", invariant=1)
    doc.build(story or [Spacer(1, 1)], onFirstPage=footer, onLaterPages=footer)
    return out.getvalue()
