"""A person's own document templates, learned from what they sent, and a new document made
in one of them from cited ontology facts.

People who send quotes, price lists, proposals, spec sheets, certificates and letters each
keep a few layouts of their own: the same sections, column headers, tier breaks, terms
lines and sign-off, with the values changed. This module is the pure part:

* **Reading** (``read``): a document as units in reading order, each a paragraph or a
  table cell (``.docx``: the body, headers, footers, footnotes, endnotes and text boxes,
  review comments emptied; ``.xlsx``: every cell of every
  sheet; PDF: the text layer's lines, a line of two or more spaced columns read as a table
  row). Decided from the bytes, never the name.
* **Values** (``values_in``, ``shape``): money, dates, quantities with a unit, percentages,
  plain numbers, identifiers and addresses in a unit's text. A unit's *shape* is its text
  with each value replaced by its type (``Total {money}``).
* **Templates** (``cluster``, ``learn``): one author's documents of one kind are grouped by
  structure (shared shapes, ``SIMILAR``) and a group of ``MIN_MEMBERS`` or more becomes a
  template, learned from its latest ``MAX_MEMBERS`` documents with the latest as the
  exemplar. Each exemplar unit is ``fixed`` (its text recurs in ``FIXED_SHARE`` of the
  members, sent to two or more counterparties, and names none of the parties, subjects or
  record facts of the members holding it: re-quotes to one customer never make that
  customer's name fixed; money and dates never are), a ``slot`` (its shape recurs the same
  way, its values change) or
  ``variable`` (its text changes: another customer's names and items); slots and variable
  units carry a label (``Label: value``, the column header above, the label cell to the
  left). Tables carry their header, column roles and **tier structure**: quantity breaks
  across the header (``25 kg | 100 kg``) or down a quantity column beside a price column.
  **Conventions** are read from every member's values: currency sign and code and where
  the code stands, decimals, thousands separators, the rounding step prices keep, the date
  format and the units used. The **sign-off** is the run of fixed units that ends the
  exemplar; **terms** are fixed units that state terms (``TERMS``).
* **Generation** (``generate``): the exemplar with every fixed unit as it is, each slot or
  variable unit with a label filled through the form-fill engine (``doc_fill.plan``: the
  label's rule, one distinct value among the cited facts, else left) and formatted by the
  template's conventions, a date slot labelled as the document's date given the request's
  date, a tier table cut to one item's rows with its breaks kept, and everything else
  blanked (``____``) and flagged. Nothing the exemplar said about its own customer
  reaches the new document: a unit that is not fixed text is either filled from a fact or
  blank. ``.docx`` and ``.xlsx`` exemplars are rewritten in place (layout, styles and
  logos kept); a PDF exemplar gives a plain ``.docx`` with its units in order.
* **Comparison** (``compare``): a generated document against the one the person actually
  sent (held out), on structure, wording, value conventions, tiers and sign-off.

Nothing here reads a source, sends or writes. Evidence label of tests: ``isolated-fixture``.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
import datetime as dt
from decimal import Decimal, InvalidOperation
import difflib
import hashlib
import io
import json
from pathlib import Path
import re
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape
import zipfile

from operating import doc_fill as df

SCHEMA = "plx-doc-style/v2"   # v2: fixed text spans two parties (v1 files refused)
KINDS_FILE = Path(__file__).with_name("doc_style_kinds.json")
KINDS_SCHEMA = "plx-doc-style-kinds/v1"
MIN_MEMBERS = 2
MAX_MEMBERS = 12
SIMILAR = 0.35
FIXED_SHARE = 0.6
MAX_UNITS = 3000
MAX_UNIT_TEXT = 400
MAX_SIGNOFF = 8
BLANK = "____"
# a pack size, not an order quantity: never a tier break
SIZE_UNITS = frozenset({"ml", "l", "g", "oz", "gal", "fl"})
STATUSES = ("kept", "filled", "dated", "blank", "removed")
DATE_LABELS = frozenset({"date", "quote date", "issue date", "issued", "dated", "date issued",
                         "date of issue", "quotation date", "prepared on", "effective date"})
TERMS = re.compile(r"(?i)\b(terms?|f\.?o\.?b|ex ?works|exw|ddp|dap|incoterms?|net ?\d+|payment|"
                   r"valid(ity)?|lead ?time|subject to|minimum order|moq|freight|taxes|"
                   r"prices? (are|is|do)|quotation is|quote is)\b")

_W = df._W
_S = df._S


class StyleRefused(ValueError):
    """Nothing can be made: ``code`` says why (the API words it)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# ---- values ------------------------------------------------------------------------------

_UNIT_WORDS = (r"kgs?|kilos?|kilograms?|g|grams?|lbs?|pounds?|mt|tonnes?|tons?|oz|l|litres?|"
               r"liters?|ml|gal|gallons?|units?|pcs|pieces?|ea|each|cases?|drums?|pails?|"
               r"totes?|bags?|boxes|bottles?|jars?|tubes?")
_NUM = r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?|\.\d+"
_CODES = r"USD|CAD|EUR|GBP|AUD|MXN|CNY|JPY"
_SIGN = r"US\$|C\$|CA\$|CAD\$|CDN\$|\$|€|£|¥"
_MONTHS = r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|" \
          r"sept?(?:ember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?"
# (format, regex): the first that matches a span decides its format
_DATES: tuple[tuple[str, str], ...] = (
    ("%Y-%m-%d", r"(?:19|20)\d\d-\d{1,2}-\d{1,2}"),
    ("%Y/%m/%d", r"(?:19|20)\d\d/\d{1,2}/\d{1,2}"),
    ("%m/%d/%Y", r"\d{1,2}/\d{1,2}/(?:19|20)\d\d"),
    ("%m/%d/%y", r"\d{1,2}/\d{1,2}/\d\d"),
    ("%d-%b-%Y", rf"\d{{1,2}}-(?:{_MONTHS})-(?:19|20)\d\d"),
    ("%d-%b-%y", rf"\d{{1,2}}-(?:{_MONTHS})-\d\d"),
    ("%B %d, %Y", rf"(?:{_MONTHS})\.? \d{{1,2}}(?:st|nd|rd|th)?,? (?:19|20)\d\d"),
    ("%d %B %Y", rf"\d{{1,2}}(?:st|nd|rd|th)? (?:{_MONTHS})\.?,? (?:19|20)\d\d"),
)
_VALUE = re.compile(
    r"(?P<email>[A-Za-z0-9._%+'-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)"
    r"|(?P<url>(?:https?://|www\.)\S+)"
    r"|(?P<date>" + "|".join(f"(?:{p})" for _f, p in _DATES) + r")"
    rf"|(?P<money>(?:(?P<sign>{_SIGN})\s?(?P<code1>{_CODES})?\s?(?P<mnum>-?(?:{_NUM}))"
    rf"(?:\s?(?P<code4>{_CODES})\b)?"
    rf"|(?P<code2>{_CODES})\s?(?P<sign2>\$)?\s?(?P<mnum2>-?(?:{_NUM}))"
    rf"|(?P<mnum3>-?(?:{_NUM}))\s?(?P<code3>{_CODES}))"
    rf"(?:\s?/\s?(?P<per>{_UNIT_WORDS}))?)"
    rf"|(?P<pct>(?:{_NUM})\s?%)"
    rf"|(?P<qty>(?P<qnum>(?:{_NUM}))(?:\s?[-–]\s?(?:{_NUM}))?\s?\+?\s?(?P<unit>{_UNIT_WORDS})"
    r"(?![A-Za-z]))"
    r"|(?P<id>\b(?=[A-Za-z0-9/-]*\d)(?=[A-Za-z0-9/-]*[A-Za-z])[A-Za-z0-9][A-Za-z0-9/-]{3,}\b)"
    rf"|(?P<num>(?<![\w.])(?:{_NUM})(?![\w]))",
    re.I)


@dataclass(frozen=True)
class Value:
    type: str                 # money | date | qty | pct | num | id | email | url
    raw: str
    start: int
    end: int
    number: Optional[Decimal] = None
    unit: str = ""            # qty unit, or money's per-unit
    sign: str = ""            # money: $, €, £ ...
    code: str = ""            # money: USD, CAD ...
    code_after: bool = False
    date_format: str = ""


def _dec(text: Optional[str]) -> Optional[Decimal]:
    if not text:
        return None
    try:
        return Decimal(text.replace(",", ""))
    except InvalidOperation:
        return None


def _unit(word: str) -> str:
    w = word.lower().rstrip(".")
    for canon, forms in (("kg", ("kg", "kgs", "kilo", "kilos", "kilogram", "kilograms")),
                         ("lb", ("lb", "lbs", "pound", "pounds")), ("g", ("g", "gram", "grams")),
                         ("l", ("l", "litre", "litres", "liter", "liters")),
                         ("gal", ("gal", "gallon", "gallons")), ("mt", ("mt", "tonne", "tonnes")),
                         ("ea", ("ea", "each", "unit", "units", "pcs", "piece", "pieces"))):
        if w in forms:
            return canon
    return w.rstrip("s") if len(w) > 3 else w


def values_in(text: str) -> list[Value]:
    """Every value in ``text`` in order (module doc, **Values**)."""
    out = []
    for m in _VALUE.finditer(text or ""):
        kind = m.lastgroup if m.lastgroup in ("email", "url", "date", "pct", "id", "num") \
            else None
        if m.group("money"):
            num = m.group("mnum") or m.group("mnum2") or m.group("mnum3")
            code = (m.group("code1") or m.group("code2") or m.group("code3")
                    or m.group("code4") or "").upper()
            out.append(Value("money", m.group(0), m.start(), m.end(), _dec(num),
                             _unit(m.group("per")) if m.group("per") else "",
                             m.group("sign") or m.group("sign2") or "", code,
                             bool(m.group("code3") or m.group("code4"))))
        elif m.group("qty"):
            out.append(Value("qty", m.group(0), m.start(), m.end(), _dec(m.group("qnum")),
                             _unit(m.group("unit"))))
        elif m.group("date"):
            fmt = next(f for f, p in _DATES if re.fullmatch(p, m.group(0), re.I))
            out.append(Value("date", m.group(0), m.start(), m.end(), date_format=fmt))
        elif m.group("pct"):
            out.append(Value("pct", m.group(0), m.start(), m.end(),
                             _dec(m.group(0).rstrip("% "))))
        elif kind == "num":
            out.append(Value("num", m.group(0), m.start(), m.end(), _dec(m.group(0))))
        elif kind:
            out.append(Value(kind, m.group(0), m.start(), m.end()))
    return out


def squash(text: str) -> str:
    return " ".join(str(text or "").split()).lower()


def shape(text: str, found: Optional[Sequence[Value]] = None) -> str:
    """``text`` with each value replaced by ``{type}``, lower case, single spaces."""
    found = values_in(text) if found is None else found
    parts, at = [], 0
    for v in found:
        parts += [text[at:v.start], "{" + v.type + "}"]
        at = v.end
    parts.append(text[at:])
    return squash("".join(parts))


def _wordy(text: str) -> bool:
    return len(re.findall(r"[A-Za-z]", text)) >= 2


# ---- reading -----------------------------------------------------------------------------

@dataclass(frozen=True)
class Unit:
    uid: str                  # docx:document:p3, docx:document:t0:r2:c1, xlsx:Sheet1!B4, pdf:1:7:0
    text: str
    block: str                # "" for a paragraph; the table, sheet or PDF page otherwise
    row: int = -1
    col: int = -1


@dataclass
class Reading:
    format: str               # docx | xlsx | pdf
    units: list[Unit]
    setters: dict[str, Callable[[str], None]] = field(default_factory=dict, repr=False)
    rows: dict[tuple[str, int], Callable[[], None]] = field(default_factory=dict, repr=False)
    write: Optional[Callable[[], bytes]] = field(default=None, repr=False)


def _clip(text: str) -> str:
    return " ".join(str(text or "").split())[:MAX_UNIT_TEXT]


_MC = "{http://schemas.openxmlformats.org/markup-compatibility/2006}"
_TXBX = _W + "txbxContent"
_COMMENT_MARKS = {_W + "commentRangeStart", _W + "commentRangeEnd"}
# review comments are never part of a template: their parts are emptied
_COMMENT_PART = re.compile(r"word/comments(Extended|Ids|Extensible)?\.xml")


def _own_ts(p: ET.Element) -> list[ET.Element]:
    """A paragraph's own text nodes: not those of a text box drawn in it."""
    out: list[ET.Element] = []

    def visit(el: ET.Element) -> None:
        for child in el:
            if child.tag == _TXBX:
                continue
            if child.tag == _W + "t":
                out.append(child)
            visit(child)
    visit(p)
    return out


def _boxes(p: ET.Element) -> list[ET.Element]:
    """The text boxes drawn in a paragraph (not those inside another box: its own
    paragraphs hold them)."""
    out: list[ET.Element] = []

    def visit(el: ET.Element) -> None:
        for child in el:
            if child.tag == _TXBX:
                out.append(child)
            else:
                visit(child)
    visit(p)
    return out


def _set_own(p: ET.Element, value: str) -> None:
    ts = _own_ts(p)
    if not ts:
        df._set_text(ET.SubElement(p, _W + "r"), value)
        return
    ts[0].text = value
    ts[0].set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    for t in ts[1:]:
        t.text = ""


def _scrub_markup(root: ET.Element) -> None:
    """Comment marks and references removed; an ``mc:AlternateContent`` keeps its first
    choice only (its fallback copy of a text box would keep the old text)."""
    for parent in list(root.iter()):
        for child in list(parent):
            if child.tag in _COMMENT_MARKS:
                parent.remove(child)
            elif child.tag == _W + "r" and child.find(_W + "commentReference") is not None:
                parent.remove(child)
            elif child.tag == _MC + "Fallback" and parent.tag == _MC + "AlternateContent" \
                    and parent.find(_MC + "Choice") is not None:
                parent.remove(child)


def _read_docx(data: bytes) -> Reading:
    reading = Reading("docx", [])
    parts: dict[str, tuple[ET.Element, bytes]] = {}
    emptied: dict[str, bytes] = {}
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        listed = z.namelist()
        names = [n for n in listed if n == "word/document.xml" or re.fullmatch(
            r"word/(header|footer)\d*\.xml|word/(footnotes|endnotes)\.xml", n)]
        for name in sorted(names, key=lambda n: (n != "word/document.xml", n)):
            raw = z.read(name)
            df._register(raw)
            try:
                parts[name] = (ET.fromstring(raw), raw)
            except ET.ParseError:
                raise StyleRefused("unreadable") from None
            _scrub_markup(parts[name][0])
        for name in listed:
            if _COMMENT_PART.fullmatch(name):
                raw = z.read(name)
                head = re.search(rb"<(?![?!])[^>]*?(/?)>", raw)
                if head is None:
                    raise StyleRefused("unreadable")
                tag = re.match(rb"<([^\s>/]+)", head.group(0)).group(1)
                start = head.group(0)[:-2] + b">" if head.group(1) else head.group(0)
                emptied[name] = raw[:head.start()] + start + b"</" + tag + b">"
    for name, (root, _raw) in parts.items():
        short = name.rsplit("/", 1)[-1][:-4]
        counter = {"p": 0, "t": 0}

        def walk(el: ET.Element, parent: ET.Element) -> None:
            for child in el:
                if child.tag == _W + "tbl":
                    t = counter["t"]
                    counter["t"] += 1
                    block = f"{short}:t{t}"
                    for r, tr in enumerate(child.findall(_W + "tr")):
                        reading.rows[(block, r)] = (lambda tbl=child, tr=tr: tbl.remove(tr)
                                                    if tr in list(tbl) else None)
                        for c, tc in enumerate(tr.findall(_W + "tc")):
                            text = "\n".join(df._text(p) for p in tc.iter(_W + "p"))
                            uid = f"docx:{block}:r{r}:c{c}"
                            reading.units.append(Unit(uid, _clip(text), block, r, c))
                            reading.setters[uid] = lambda v, tc=tc: df._set_text(tc, v)
                elif child.tag == _W + "p":
                    n = counter["p"]
                    counter["p"] += 1
                    uid = f"docx:{short}:p{n}"
                    own = "".join(t.text or "" for t in _own_ts(child))
                    reading.units.append(Unit(uid, _clip(own), "", n))
                    reading.setters[uid] = lambda v, p=child: _set_own(p, v)
                    # a text box drawn in the paragraph: its paragraphs are units too
                    for box in _boxes(child):
                        walk(box, child)
                else:
                    walk(child, el)
        walk(root, root)

    def write() -> bytes:
        return df._rezip(data, {**{n: df._serialize(r, raw) for n, (r, raw) in parts.items()},
                                **emptied, **_scrubbed_props(data)})
    reading.write = write
    return reading


def _scrubbed_props(data: bytes) -> dict[str, bytes]:
    """The package's title, subject, keywords and description emptied (they name the
    document the exemplar was); its author kept."""
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        if "docProps/core.xml" not in z.namelist():
            return {}
        raw = z.read("docProps/core.xml")
    return {"docProps/core.xml": re.sub(
        rb"(<(dc:title|dc:subject|cp:keywords|dc:description|cp:category)\b[^>]*>)[^<]*(</\2>)",
        rb"\1\3", raw)}


_BUILTIN_FORMATS = {5: "$#,##0", 6: "$#,##0", 7: "$#,##0.00", 8: "$#,##0.00",
                    14: "mm/dd/yyyy", 15: "d-mmm-yy", 16: "d-mmm", 17: "mmm-yy",
                    22: "mm/dd/yyyy h:mm", 42: "$#,##0", 44: "$#,##0.00"}
_EXCEL_DAY0 = dt.date(1899, 12, 30)


def _number_formats(z: zipfile.ZipFile, names: set[str]) -> list[str]:
    """Each cell style's number format code, by style index."""
    if "xl/styles.xml" not in names:
        return []
    try:
        root = ET.fromstring(z.read("xl/styles.xml"))
    except ET.ParseError:
        return []
    custom = {int(f.get("numFmtId") or -1): f.get("formatCode") or ""
              for f in root.iter(_S + "numFmt")}
    xfs = root.find(_S + "cellXfs")
    out = []
    for xf in (xfs if xfs is not None else ()):
        n = int(xf.get("numFmtId") or 0)
        out.append(custom.get(n) or _BUILTIN_FORMATS.get(n, ""))
    return out


def _strftime_of(code: str) -> str:
    out = code.split(";")[0].replace("\\", "").replace('"', "")
    out = re.sub(r"\[[^\]]*\]", "", out)
    for a, b in (("yyyy", "%Y"), ("yy", "%y"), ("mmmm", "%B"), ("mmm", "%b"), ("mm", "%m"),
                 ("dd", "%d")):
        out = out.replace(a, b)
    out = re.sub(r"(?<!%)m", "%m", out)
    out = re.sub(r"(?<!%)d", "%d", out)
    return out.split(" h")[0].strip()


def _display(value: str, code: str) -> str:
    """A numeric cell as Excel shows it: a money format's sign, code and decimals, a date
    format's date. Anything else as stored."""
    number = _dec(value)
    if number is None or not code or code.lower() == "general":
        return value
    first = code.split(";")[0]
    if re.search(r"[$€£¥]|\[\$[A-Z]{3}", first):
        sign = next((c for c in "$€£¥" if c in re.sub(r"\[\$[A-Z]{3}[^\]]*\]", "", first)), "")
        cur = re.search(r"\[\$(?:[$€£¥])?-?([A-Z]{3})", first)
        frac = re.search(r"\.(0+)", first)
        places = len(frac.group(1)) if frac else 0
        text = f"{number:,.{places}f}" if "," in first else f"{number:.{places}f}"
        return f"{sign}{text}" + (f" {cur.group(1)}" if cur else "")
    if re.search(r"[yd]", first.lower()) and not re.search(r"[0#]", first) and 0 < number < 80000:
        day = _EXCEL_DAY0 + dt.timedelta(days=int(number))
        try:
            return day.strftime(_strftime_of(first.lower()))
        except ValueError:
            return value
    return value


def _read_xlsx(data: bytes) -> Reading:
    reading = Reading("xlsx", [])
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = set(z.namelist())
        shared = df._Xlsx._shared(z.read("xl/sharedStrings.xml")) \
            if "xl/sharedStrings.xml" in names else []
        sheets = df._Xlsx._sheets(z, names)
        parts = {part: z.read(part) for _n, part in sheets}
        formats = _number_formats(z, names)
    edits: dict[str, bytes] = dict(parts)
    for sheet, part in sheets:
        root = ET.fromstring(parts[part])
        for c in root.iter(_S + "c"):
            m = df._CELL.match(c.get("r") or "")
            if not m:
                continue
            kind, v = c.get("t"), c.find(_S + "v")
            if kind == "s" and v is not None and (v.text or "").isdigit() \
                    and int(v.text) < len(shared):
                text = shared[int(v.text)]
            elif kind == "inlineStr":
                text = "".join(t.text or "" for t in c.iter(_S + "t"))
            else:
                text = v.text if v is not None and v.text else ""
                style = int(c.get("s") or 0)
                if kind in (None, "n") and style < len(formats):
                    text = _display(text, formats[style])
            if not text.strip():
                continue
            ref = m.group(1) + m.group(2)
            numeric = kind in (None, "n")
            uid = f"xlsx:{sheet}!{ref}"
            reading.units.append(Unit(uid, _clip(text), sheet, int(m.group(2)),
                                      df.column_number(m.group(1))))

            def put(value: str, part: str = part, ref: str = ref, numeric: bool = numeric
                    ) -> None:
                edits[part] = _put_number(edits[part], ref, value) if numeric \
                    else df._put_cell(edits[part], ref, value)
            reading.setters[uid] = put
    reading.units.sort(key=lambda u: (u.block != (sheets[0][0] if sheets else ""), u.block,
                                      u.row, u.col))
    by_row: dict[tuple[str, int], list[str]] = {}
    for u in reading.units:
        by_row.setdefault((u.block, u.row), []).append(u.uid)
    for key, uids in by_row.items():
        reading.rows[key] = lambda uids=uids: [reading.setters[u]("") for u in uids]
    reading.write = lambda: df._rezip(data, {**{p: edits[p] for p in edits
                                                if edits[p] is not parts[p]},
                                             **_scrubbed_props(data)})
    return reading


def _put_number(xml: bytes, ref: str, value: str) -> bytes:
    """A number cell keeps being a number (its style shows it): money or a plain number is
    written as the value, a date as its serial; anything else as text."""
    found = values_in(value)
    number = None
    if len(found) == 1 and found[0].raw.strip() == value.strip():
        v = found[0]
        if v.type in ("money", "num"):
            number = v.number
        elif v.type == "date":
            try:
                day = dt.datetime.strptime(v.raw, v.date_format).date()
                number = Decimal((day - _EXCEL_DAY0).days)
            except ValueError:
                number = None
    xml = df._put_cell(xml, ref, value if number is None else "\x00")
    if number is None:
        return xml
    return xml.replace(b' t="inlineStr"><is><t xml:space="preserve">\x00</t></is></c>',
                       b"><v>" + format(number, "f").encode() + b"</v></c>", 1)


def _read_pdf(data: bytes) -> Reading:
    try:
        from pypdf import PdfReader
        from pypdf.errors import PdfReadError
    except ImportError:
        raise StyleRefused("pdf_unavailable") from None
    reading = Reading("pdf", [])
    try:
        pdf = PdfReader(io.BytesIO(data))
        if pdf.is_encrypted:
            raise StyleRefused("encrypted")
        for p, page in enumerate(pdf.pages[:20]):
            try:
                text = page.extract_text(extraction_mode="layout") or ""
            except (TypeError, ValueError):
                text = page.extract_text() or ""
            for n, line in enumerate(text.splitlines()):
                cells = [c for c in re.split(r"\s{3,}", line.strip()) if c]
                for c, cell in enumerate(cells):
                    reading.units.append(Unit(f"pdf:{p}:{n}:{c}", _clip(cell),
                                              f"pdf{p}" if len(cells) > 1 else "", n,
                                              c if len(cells) > 1 else -1))
    except (PdfReadError, ValueError, KeyError, TypeError):
        raise StyleRefused("unreadable") from None
    return reading


def read(data: bytes) -> Reading:
    """The document's units (module doc, **Reading**). StyleRefused when it is none of
    ``.docx``, ``.xlsx`` or PDF or cannot be read."""
    try:
        kind = df.detect(bytes(data))
    except df.FillRefused as refused:
        raise StyleRefused(refused.code) from None
    try:
        reading = {"docx": _read_docx, "xlsx": _read_xlsx, "pdf": _read_pdf}[kind](data)
    except (KeyError, ET.ParseError, zipfile.BadZipFile):
        raise StyleRefused("unreadable") from None
    reading.units = [u for u in reading.units if u.text.strip()][:MAX_UNITS]
    return reading


# ---- tables and tiers --------------------------------------------------------------------

def _header_row(units: Sequence[Unit]) -> Optional[int]:
    """The block's header row: the first of its rows with two or more worded cells and no
    money or date, with a row after it."""
    rows: dict[int, list[Unit]] = {}
    for u in units:
        rows.setdefault(u.row, []).append(u)
    ordered = sorted(rows)
    for i, r in enumerate(ordered[:15]):
        cells = rows[r]
        worded = [u for u in cells if _wordy(u.text)]
        if len(worded) >= 2 and i + 1 < len(ordered) and not any(
                v.type in ("money", "date") for u in cells for v in values_in(u.text)):
            return r
    return None


def tables_of(units: Sequence[Unit]) -> list[dict[str, Any]]:
    """Each table's header, column roles and tier structure (module doc, **Templates**)."""
    from operating.document_tables import role_of
    blocks: dict[str, list[Unit]] = {}
    for u in units:
        if u.block:
            blocks.setdefault(u.block, []).append(u)
    out = []
    for block, cells in blocks.items():
        head = _header_row(cells)
        if head is None:
            continue
        header = {u.col: u for u in cells if u.row == head}
        per_row: dict[int, int] = Counter(u.row for u in cells if u.row > head)
        last = head
        for r in sorted(per_row):
            if r > last + 1 or per_row[r] < 2:
                break                   # a gap or a lone cell: the table has ended
            last = r
        body = [u for u in cells if head < u.row <= last]
        columns = []
        tier_cols = []
        for col, u in sorted(header.items()):
            qty = [v for v in values_in(u.text) if v.type == "qty"]
            role = role_of(re.sub(r"\(.*?\)", "", u.text)) or ""
            if qty and qty[0].unit not in SIZE_UNITS:
                tier_cols.append((qty[0].number, qty[0].unit))
                role = "tier"
            elif re.search(r"(?i)\b(tier|break|moq|min(imum)?\.? (order|qty))\b", u.text):
                role = "quantity"
            columns.append({"col": col, "header": u.text, "role": role})
        tiers: dict[str, Any] = {}
        prices = any(c["role"] in ("price", "tier") for c in columns) or any(
            v.type == "money" for u in body for v in values_in(u.text))
        rising = all(a[0] is not None and b[0] is not None and a[0] < b[0]
                     for a, b in zip(tier_cols, tier_cols[1:]))
        if len(tier_cols) >= 2 and rising and prices:
            tiers = {"axis": "column", "breaks": [str(n) for n, _u in tier_cols],
                     "unit": Counter(u for _n, u in tier_cols).most_common(1)[0][0]}
        else:
            qcol = next((c["col"] for c in columns if c["role"] == "quantity"), None)
            if qcol is not None and prices:
                seq: list[Value] = []
                for u in sorted((u for u in body if u.col == qcol), key=lambda u: u.row):
                    got = [v for v in values_in(u.text) if v.type in ("qty", "num")]
                    if not got:
                        continue
                    if seq and got[0].number is not None and seq[-1].number is not None \
                            and got[0].number <= seq[-1].number:
                        break                       # the next item's breaks begin
                    seq.append(got[0])
                if len(seq) >= 2:
                    tiers = {"axis": "row", "breaks": [str(v.number) for v in seq],
                             "unit": Counter(v.unit for v in seq).most_common(1)[0][0]}
        out.append({"block": block, "header_row": head, "last_row": last,
                    "columns": columns, "tiers": tiers, "body_rows": len({u.row for u in body})})
    return out


# ---- conventions -------------------------------------------------------------------------

_STEPS = (Decimal("1"), Decimal("0.5"), Decimal("0.25"), Decimal("0.1"), Decimal("0.05"),
          Decimal("0.01"))


def conventions(units_of_members: Iterable[Sequence[Unit]]) -> dict[str, Any]:
    """How the members write money, dates, quantities and percentages (module doc)."""
    money: list[Value] = []
    dates: Counter[str] = Counter()
    units: Counter[str] = Counter()
    pct: Counter[int] = Counter()
    stated: Counter[str] = Counter()
    for units_ in units_of_members:
        for u in units_:
            stated.update(re.findall(rf"\b({_CODES})\b", u.text))
            for v in values_in(u.text):
                if v.type == "money" and v.number is not None:
                    money.append(v)
                elif v.type == "date":
                    dates[v.date_format] += 1
                elif v.type == "qty":
                    units[v.unit] += 1
                elif v.type == "pct":
                    pct[len(v.raw.rstrip("% ").partition(".")[2])] += 1
    out: dict[str, Any] = {"money_values": len(money)}
    if stated:
        out["stated"] = stated.most_common(1)[0][0]
    if money:
        signs = Counter(v.sign for v in money)
        codes = Counter(v.code for v in money if v.code)
        places = Counter(len(v.raw.replace(v.code, "").split(".")[1].split("/")[0].strip())
                         if "." in v.raw else 0 for v in money)
        big = [v for v in money if v.number is not None and abs(v.number) >= 1000]
        steps = {}
        for step in _STEPS:
            steps[str(step)] = sum(1 for v in money if v.number % step == 0) / len(money)
        out.update({
            "sign": signs.most_common(1)[0][0],
            "code": codes.most_common(1)[0][0] if codes else "",
            "code_after": (sum(1 for v in money if v.code and v.code_after)
                           > sum(1 for v in money if v.code and not v.code_after)),
            "decimals": places.most_common(1)[0][0],
            "thousands": (sum(1 for v in big if "," in v.raw) * 2 >= len(big)) if big else True,
            "rounding": next((s for s, share in steps.items() if share >= 0.9), "0.01"),
            "per_unit": Counter(v.unit for v in money if v.unit).most_common(1)[0][0]
            if any(v.unit for v in money) else "",
        })
    if dates:
        out["date_format"] = dates.most_common(1)[0][0]
    if units:
        out["units"] = [u for u, _n in units.most_common(3)]
    if pct:
        out["percent_decimals"] = pct.most_common(1)[0][0]
    return out


def format_money(amount: Decimal, conv: Mapping[str, Any], currency: str = "") -> str:
    """``amount`` as the template writes money: its rounding step, decimals, thousands
    separator, sign and code. A currency other than the template's is written as its code."""
    step = Decimal(str(conv.get("rounding") or "0.01"))
    amount = (amount / step).quantize(Decimal("1"), rounding="ROUND_HALF_UP") * step
    places = int(conv.get("decimals", 2))
    text = f"{amount:,.{places}f}" if conv.get("thousands", True) else f"{amount:.{places}f}"
    code = conv.get("code") or ""
    if currency and currency != (code or conv.get("stated") or ""):
        return f"{text} {currency}"       # not the currency the template writes
    sign = conv.get("sign") or ""
    if code:
        return f"{sign}{text} {code}" if conv.get("code_after") else f"{code} {sign}{text}"
    return f"{sign}{text}"


def format_date(day: dt.date, conv: Mapping[str, Any]) -> str:
    fmt = str(conv.get("date_format") or "%Y-%m-%d")
    text = day.strftime(fmt)
    return re.sub(r"(?<![\d])0(\d)(?=[/,\- ])", r"\1", text) if fmt in (
        "%m/%d/%Y", "%m/%d/%y", "%B %d, %Y", "%d %B %Y") else text


# ---- templates ---------------------------------------------------------------------------

@dataclass
class Member:
    source: str               # the L1 handle of the sent copy
    sent: str                 # ISO time it was sent
    digest: str               # sha256 of its bytes
    units: list[Unit]
    format: str
    # who it went to (an outside address's domain, or a record id); "" when unknown
    counterparty: str = ""
    # its recipients' addresses and names and its record's facts (customer name, number):
    # text naming one of them is that document's own, never a template's fixed text
    names: tuple[str, ...] = ()
    subject: str = ""


# mailbox providers: the address, not its domain, names the party
_PROVIDERS = frozenset({"gmail", "googlemail", "outlook", "hotmail", "live", "msn", "yahoo",
                        "icloud", "me", "aol", "protonmail", "proton", "gmx", "mail"})


def party_of(addresses: Iterable[str], own_domains: Iterable[str]) -> str:
    """The counterparty a sent document went to: the first outside recipient's domain (the
    address itself at a mailbox provider); "" when none is outside."""
    own = {d.lower() for d in own_domains}
    for a in addresses:
        a = str(a or "").strip().lower()
        local, at, domain = a.rpartition("@")
        if not at or not local or domain in own:
            continue
        return a if domain.split(".")[0] in _PROVIDERS else domain
    return ""


def _name_terms(names: Iterable[str]) -> set[str]:
    """What text naming a party looks like: each name as written (squashed) and, for an
    address, its domain's first label (``northwind`` of ``buyer@northwind.com``)."""
    out: set[str] = set()
    for n in names:
        n = squash(n)
        if "@" in n:
            local, _, domain = n.rpartition("@")
            stem = domain.split(".")[0]
            if len(stem) >= 4 and stem not in _PROVIDERS:
                out.add(stem)
            if len(local) >= 5:
                out.add(local)
            continue
        if len(n) >= 3 and _wordy(n):
            out.add(n)
    return out


def _subject_words(members: Sequence["Member"]) -> list[set[str]]:
    """Each member's subject words that no member to another party shares: the words that
    name this document's own customer or job (``Quote`` recurs everywhere; a name does not)."""
    words = [set(re.findall(r"[a-z][a-z0-9&'-]{3,}", squash(m.subject))) for m in members]
    out = []
    for i, m in enumerate(members):
        others: set[str] = set()
        for j, n in enumerate(members):
            if j != i and (not m.counterparty or n.counterparty != m.counterparty):
                others |= words[j]
        out.append(words[i] - others)
    return out


def _names_in(text: str, terms: Iterable[str]) -> bool:
    t = squash(text)
    return any(re.search(r"(?<![a-z0-9])" + re.escape(n) + r"(?![a-z0-9])", t)
               for n in terms)


def fingerprint(units: Sequence[Unit]) -> frozenset[str]:
    return frozenset(s for s in (shape(u.text) for u in units) if _wordy(s) and len(s) <= 160)


def similarity(a: frozenset[str], b: frozenset[str]) -> float:
    return len(a & b) / len(a | b) if a or b else 0.0


def cluster(members: Sequence[Member]) -> list[list[Member]]:
    """One author's documents of one kind grouped by structure, oldest first: each joins
    the group holding the document it is most like (``SIMILAR`` or more), else starts one."""
    groups: list[list[tuple[Member, frozenset[str]]]] = []
    for m in sorted(members, key=lambda x: x.sent):
        if m.format not in ("docx", "xlsx", "pdf"):
            continue
        fp = fingerprint(m.units)
        if not fp:
            continue
        best, score = None, SIMILAR
        for g in groups:
            s = max(similarity(fp, f) for _m, f in g[-MAX_MEMBERS:])
            if s >= score:
                best, score = g, s
        if best is None:
            groups.append([(m, fp)])
        else:
            best.append((m, fp))
    return [[m for m, _f in g] for g in groups]


def _label(u: Unit, fixed: Mapping[tuple[str, int, int], str],
           headers: Mapping[tuple[str, int], str]) -> str:
    """What a changing unit is: its own ``Label:``, the fixed label cell to its left, or
    its column's fixed header. Never text that itself changes (another record's name)."""
    head, sep, _rest = u.text.partition(":")
    if sep and 1 < len(head) <= 40 and _wordy(head) and not values_in(head):
        return head.strip()
    if u.block:
        left = fixed.get((u.block, u.row, u.col - 1))
        if left is not None and _wordy(left) and len(left) <= 40 and not values_in(left):
            return left.rstrip(": ").strip()
        return headers.get((u.block, u.col), "")
    return ""


def learn(author: str, kind: str, members: Sequence[Member]) -> Optional[dict[str, Any]]:
    """One group's template (module doc, **Templates**); None for fewer than
    ``MIN_MEMBERS`` documents."""
    members = sorted(members, key=lambda m: m.sent)[-MAX_MEMBERS:]
    if len(members) < MIN_MEMBERS:
        return None
    ex = members[-1]
    texts = [Counter(squash(u.text) for u in m.units) for m in members]
    shapes = [Counter(shape(u.text) for u in m.units) for m in members]
    need = max(MIN_MEMBERS, FIXED_SHARE * len(members))
    subject_words = _subject_words(members)
    own = [_name_terms(m.names) | subject_words[i] for i, m in enumerate(members)]

    def recurs(held: Sequence[bool], text: str) -> bool:
        """In ``need`` members, sent to two or more parties, naming none of them: a
        customer's name repeated across re-quotes to that customer is theirs, not fixed."""
        holders = [i for i, h in enumerate(held) if h]
        parties = {members[i].counterparty for i in holders if members[i].counterparty}
        return len(holders) >= need and len(parties) >= 2 and not any(
            _names_in(text, own[i]) for i in holders)
    tables = tables_of(ex.units)
    head_rows = {(t["block"], t["header_row"]) for t in tables}
    roles: dict[str, str] = {}
    for u in ex.units:
        found = values_in(u.text)
        sh = shape(u.text, found)
        changing = any(v.type in ("money", "date") for v in found)
        if not changing and recurs([bool(t[squash(u.text)]) for t in texts], u.text):
            roles[u.uid] = "fixed"
        elif found and _wordy(sh.replace("{", "").replace("}", "")) and recurs(
                [bool(s[sh]) for s in shapes], sh):
            roles[u.uid] = "slot"
        else:
            roles[u.uid] = "variable"
    fixed = {(u.block, u.row, u.col): u.text for u in ex.units
             if u.block and roles[u.uid] == "fixed"}
    headers = {(t["block"], c["col"]): c["header"] for t in tables for c in t["columns"]
               if fixed.get((t["block"], t["header_row"], c["col"])) == c["header"]}
    units = []
    for u in ex.units:
        role = roles[u.uid]
        found = values_in(u.text)
        entry: dict[str, Any] = {"uid": u.uid, "role": role, "block": u.block, "row": u.row,
                                 "col": u.col}
        if role == "fixed":
            entry["text"] = u.text
        else:
            entry["shape"] = shape(u.text, found) if role == "slot" else ""
            entry["types"] = [v.type for v in found]
            label = _label(u, fixed, headers)
            # a label naming the exemplar's own party (``Northwind code:``) is not kept
            entry["label"] = "" if label and _names_in(label, own[-1]) else label
            entry["header"] = (u.block, u.row) in head_rows
        units.append(entry)
    signoff: list[str] = []
    for entry in reversed(units):
        if entry["role"] != "fixed" or len(signoff) >= MAX_SIGNOFF:
            break
        signoff.insert(0, entry["uid"])
    member_tiers = [[t["tiers"] for t in tables_of(m.units) if t["tiers"]] for m in members]
    tier_sets = Counter(json.dumps(t[0], sort_keys=True) for t in member_tiers if t)
    fps = [fingerprint(m.units) for m in members]
    cohesion = [similarity(fps[-1], f) for f in fps[:-1]]
    return {
        "id": hashlib.sha256("|".join([author, kind] + [m.source for m in members]).encode()
                             ).hexdigest()[:16],
        "author": author, "kind": kind, "format": ex.format,
        "members": [{"source": m.source, "sent": m.sent} for m in members],
        "exemplar": {"source": ex.source, "sent": ex.sent, "digest": ex.digest},
        "units": units, "tables": tables,
        "tiers": ({"usual": json.loads(tier_sets.most_common(1)[0][0]),
                   "share": round(tier_sets.most_common(1)[0][1] / len(members), 3)}
                  if tier_sets else {}),
        "conventions": conventions(m.units for m in members),
        "signoff": signoff,
        "terms": [e["uid"] for e in units if e["role"] == "fixed" and TERMS.search(e["text"])],
        "support": len(members),
        "cohesion": round(sum(cohesion) / len(cohesion), 3) if cohesion else 1.0,
    }


def learn_all(docs: Iterable[tuple[str, str, Member]]) -> list[dict[str, Any]]:
    """Templates for every (author, kind): ``docs`` are (author, kind, member)."""
    grouped: dict[tuple[str, str], list[Member]] = {}
    for author, kind, member in docs:
        grouped.setdefault((author.lower(), kind), []).append(member)
    out = []
    for (author, kind), members in sorted(grouped.items()):
        for group in cluster(members):
            t = learn(author, kind, group)
            if t is not None:
                out.append(t)
    return out


# ---- kinds -------------------------------------------------------------------------------

def load_kinds(path: Optional[Path] = None) -> list[tuple[str, re.Pattern[str]]]:
    """(kind, pattern on a file name) in order: the first that matches names the kind."""
    try:
        raw = json.loads(Path(path or KINDS_FILE).read_text(encoding="utf-8"))
        if raw.get("schema") != KINDS_SCHEMA:
            raise ValueError("schema")
        return [(str(k["kind"]), re.compile(str(k["name"]), re.I)) for k in raw["kinds"]]
    except (OSError, ValueError, KeyError, TypeError, re.error) as error:
        raise StyleRefused("kinds_invalid") from error


def kind_of(name: str, kinds: Sequence[tuple[str, re.Pattern[str]]]) -> Optional[str]:
    words = re.sub(r"[_\-.]+", " ", str(name or "").lower())
    return next((k for k, pattern in kinds if pattern.search(words)), None)


# ---- store -------------------------------------------------------------------------------

def save(path: Path, templates: Sequence[Mapping[str, Any]], learned_at: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps({"schema": SCHEMA, "learned_at": learned_at,
                               "templates": list(templates)}), encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(path)


def load(path: Path) -> list[dict[str, Any]]:
    """The learned templates; a file this code does not understand gives none
    (``StyleRefused("templates_invalid")``), never a guess."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        if raw.get("schema") != SCHEMA or not isinstance(raw.get("templates"), list):
            raise ValueError("schema")
        for t in raw["templates"]:
            if not (t["id"] and t["author"] and t["kind"] and t["members"]
                    and t["exemplar"]["digest"] and isinstance(t["units"], list)):
                raise ValueError("template")
        return raw["templates"]
    except FileNotFoundError:
        raise StyleRefused("no_templates") from None
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise StyleRefused("templates_invalid") from error


def choose(templates: Sequence[Mapping[str, Any]], author: str, kind: str
           ) -> Optional[Mapping[str, Any]]:
    """The author's template of that kind they use most, the latest on a tie."""
    mine = [t for t in templates if t["author"] == author.lower() and t["kind"] == kind]
    return max(mine, key=lambda t: (t["support"], t["exemplar"]["sent"]), default=None)


# ---- generation --------------------------------------------------------------------------

@dataclass(frozen=True)
class Made:
    uid: str
    label: str
    role: str                  # the template's: fixed | slot | variable
    text: str                  # what the new document holds there
    status: str                # STATUSES
    field: Optional[str] = None
    citation: Optional[df.Citation] = None
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"uid": self.uid, "label": self.label, "role": self.role, "text": self.text,
                "status": self.status, "field": self.field, "reason": self.reason,
                "citation": self.citation.as_dict() if self.citation else None}


@dataclass(frozen=True)
class Generated:
    template: str
    format: str               # docx | xlsx (a PDF exemplar gives a docx)
    data: bytes
    units: tuple[Made, ...]
    digest: str

    def counts(self) -> dict[str, int]:
        out = {s: 0 for s in STATUSES}
        for u in self.units:
            out[u.status] += 1
        return out


def _formatted(value: str, types: Sequence[str], conv: Mapping[str, Any]) -> str:
    """A fact's text (``doc_fill.format_value``) written the template's way."""
    m = re.fullmatch(r"(-?\d+(?:\.\d+)?)(?: ([A-Z]{3}))?", value)
    if m and (m.group(2) or "money" in types):
        return format_money(Decimal(m.group(1)), conv, m.group(2) or "")
    d = re.fullmatch(r"(\d{4})-(\d\d)-(\d\d)", value)
    if d:
        return format_date(dt.date(int(d.group(1)), int(d.group(2)), int(d.group(3))), conv)
    return value


def _put(entry: Mapping[str, Any], original: str, value: str) -> str:
    """The unit's new text: its label kept, its values (or all of it) replaced."""
    label = entry.get("label") or ""
    if label and squash(original).startswith(squash(label)) and ":" in original:
        return original.split(":", 1)[0] + ": " + value
    found = values_in(original)
    if entry["role"] == "slot" and len(found) == 1:
        v = found[0]
        return original[:v.start] + value + original[v.end:]
    return value


def _structural(text: str) -> bool:
    """A header or break cell that is only quantities (``25 kg``, ``1,000+``): structure,
    kept. One with other words changes from one document to the next and is not."""
    rest = re.sub(r"\{(qty|num)\}", " ", shape(text))
    return not re.search(r"[a-z]{2,}", re.sub(rf"\b({_UNIT_WORDS})\b", " ", rest))


def _tier_of(entry: Mapping[str, Any], table: Mapping[str, Any], by_uid: Mapping[str, Unit]
             ) -> Optional[str]:
    """The quoted-price fact key a price cell of a tier table stands for: the break in its
    row's quantity column (tiers down the rows) or in its column's header (across)."""
    tiers = table.get("tiers") or {}
    cols = {c["col"]: c for c in table["columns"]}
    col = cols.get(entry["col"])
    if not tiers or col is None:
        return None
    if tiers["axis"] == "column" and col["role"] == "tier":
        qty = [v for v in values_in(col["header"]) if v.type == "qty"]
    elif tiers["axis"] == "row" and col["role"] == "price":
        qcol = next(c["col"] for c in table["columns"] if c["role"] == "quantity")
        cell = next((u for u in by_uid.values() if u.block == entry["block"]
                     and u.row == entry["row"] and u.col == qcol), None)
        qty = [v for v in values_in(cell.text) if v.type in ("qty", "num")] if cell else []
    else:
        return None
    if not qty or qty[0].number is None:
        return None
    from operating.doc_gather import tier_key
    return tier_key(qty[0].number, qty[0].unit or tiers.get("unit") or "")


def _answer(keys: Sequence[str], facts: Sequence[df.Fact], conv: Mapping[str, Any],
            types: Sequence[str]) -> tuple[str, str, Optional[str], Optional[df.Citation], str]:
    """A slot answered from facts read from documents (``doc_gather``): exactly one
    distinct value under its keys, else blank with the reason."""
    values: dict[str, list[df.Fact]] = {}
    for f in facts:
        if f.key in keys:
            if f.ambiguous:
                return BLANK, "blank", None, None, "the sources disagree"
            values.setdefault(" ".join(f.value.lower().split()), []).append(f)
    if len(values) > 1:
        return BLANK, "blank", None, None, f"{len(values)} different values were found"
    if not values:
        return BLANK, "blank", None, None, ("no quoted price for this tier" if
                                            keys[0].startswith("Quoted.") else "")
    got = next(iter(values.values()))[0]
    return (_formatted(got.value, types, conv), "filled", got.key.split(".", 1)[1][:60],
            got.citation, "")


def generate(template: Mapping[str, Any], exemplar: bytes, facts: Sequence[df.Fact],
             terms: df.Terms, *, as_of: dt.date) -> Generated:
    """A new document in the template (module doc, **Generation**). StyleRefused when the
    exemplar is not the one the template was learned from (``exemplar_changed``)."""
    if hashlib.sha256(exemplar).hexdigest() != template["exemplar"]["digest"]:
        raise StyleRefused("exemplar_changed")
    reading = read(exemplar)
    by_uid = {u.uid: u for u in reading.units}
    conv = template.get("conventions") or {}
    tiers = {t["block"]: t for t in template.get("tables") or []}
    keep_rows: dict[str, set[int]] = {}
    for block, t in tiers.items():
        body = sorted({e["row"] for e in template["units"]
                       if e["block"] == block and t["header_row"] < e["row"] <= t["last_row"]})
        n = len(t["tiers"]["breaks"]) if t["tiers"].get("axis") == "row" else 1
        keep_rows[block] = set(body[:n])
    plain: list[Unit] = []
    made: list[Made] = []
    for entry in template["units"]:
        u = by_uid.get(entry["uid"])
        if u is None:
            raise StyleRefused("exemplar_changed")
        t = tiers.get(entry["block"])
        if t is not None and not t["header_row"] < entry["row"] <= t["last_row"]:
            t = None                    # outside the table: a paragraph of the sheet
        if t is not None and entry["row"] > t["header_row"] \
                and entry["row"] not in keep_rows[entry["block"]]:
            if (entry["block"], entry["row"]) in reading.rows:
                made.append(Made(u.uid, entry.get("label", ""), entry["role"], "", "removed",
                                 reason="a row of another item"))
            continue
        if entry["role"] == "fixed":
            made.append(Made(u.uid, "", "fixed", u.text, "kept"))
            plain.append(u)
            continue
        label = entry.get("label") or ""
        is_break = (t is not None and t["tiers"].get("axis") == "row" and any(
            c["role"] == "quantity" and c["col"] == entry["col"] for c in t["columns"]))
        if (entry.get("header") or is_break) and _structural(u.text):
            # a table's own header, or a tier break of the first item: structure, kept
            made.append(Made(u.uid, label, entry["role"], u.text, "kept",
                             reason="tier break" if is_break else "column header"))
            plain.append(u)
            continue
        text, status, fact_field, citation, reason = BLANK, "blank", None, None, ""
        if df.normalize(label) in DATE_LABELS or (not label and entry.get("types") == ["date"]
                                                  and entry["role"] == "slot"):
            text, status, reason = format_date(as_of, conv), "dated", "the request's date"
        elif t is not None and (tier := _tier_of(entry, t, by_uid)) is not None:
            text, status, fact_field, citation, reason = _answer(
                [tier], facts, conv, ("money",))
        elif label:
            got = df.plan([df.FormField(u.uid, label)], facts, terms)[0]
            if got.status == "filled":
                text = _formatted(got.after, entry.get("types") or (), conv)
                status, fact_field, citation = "filled", got.field, got.citation
            else:
                # a document's "Label: value" under this label or one meaning the same
                # (``doc_gather.meaning``: ``Lot No`` reads ``Lot Number``)
                from operating.doc_gather import meaning
                want = meaning(label, terms)
                keys = [f"Text.{df.normalize(label)}"] + sorted(
                    {f.key for f in facts if f.key.startswith("Text.") and want
                     and meaning(f.key[5:], terms) == want})
                text, status, fact_field, citation, reason = _answer(
                    keys, facts, conv, entry.get("types") or ())
                reason = reason or got.reason
        else:
            reason = "changes from one document to the next"
        new = _put(entry, u.text, text)
        made.append(Made(u.uid, label, entry["role"], new, status, fact_field, citation, reason))
        reading.setters.get(u.uid, lambda _v: None)(new)
        plain.append(Unit(u.uid, new, u.block, u.row, u.col))
    for m in made:
        if m.status == "removed":
            entry_row = by_uid[m.uid]
            reading.rows[(entry_row.block, entry_row.row)]()
    if reading.format == "pdf":
        data, fmt = plain_docx(plain), "docx"
    else:
        assert reading.write is not None
        data, fmt = reading.write(), reading.format
    return Generated(template["id"], fmt, data, tuple(made), hashlib.sha256(data).hexdigest())


def plain_docx(units: Sequence[Unit]) -> bytes:
    """A minimal Word document: paragraphs in order, a block of cells as a table."""
    def para(text: str) -> str:
        return (f'<w:p><w:r><w:t xml:space="preserve">{escape(text)}</w:t></w:r></w:p>')
    body: list[str] = []
    i = 0
    while i < len(units):
        u = units[i]
        if not u.block:
            body.append(para(u.text))
            i += 1
            continue
        rows: dict[int, dict[int, str]] = {}
        while i < len(units) and units[i].block == u.block:
            rows.setdefault(units[i].row, {})[units[i].col] = units[i].text
            i += 1
        width = max(max(r) for r in rows.values()) + 1
        body.append("<w:tbl><w:tblPr><w:tblStyle w:val=\"TableGrid\"/></w:tblPr>" + "".join(
            "<w:tr>" + "".join(f"<w:tc>{para(r.get(c, ''))}</w:tc>" for c in range(width))
            + "</w:tr>" for _n, r in sorted(rows.items())) + "</w:tbl>")
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml",
                   '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.'
                   'openxmlformats.org/package/2006/content-types"><Default Extension="rels" '
                   'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                   '<Default Extension="xml" ContentType="application/xml"/><Override '
                   'PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-'
                   'officedocument.wordprocessingml.document.main+xml"/></Types>')
        z.writestr("_rels/.rels",
                   '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.'
                   'openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" '
                   'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
                   'officeDocument" Target="word/document.xml"/></Relationships>')
        z.writestr("word/document.xml",
                   f'<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="{df.W}">'
                   f'<w:body>{"".join(body)}</w:body></w:document>')
    return out.getvalue()


def made_name(template: Mapping[str, Any], record_label: str, fmt: str) -> str:
    """The new file's name: the kind and the record, only characters an upload takes."""
    stem = re.sub(r"[^A-Za-z0-9._() -]+", " ", f"{template['kind'].replace('_', ' ')} "
                  f"{record_label}").strip()
    stem = re.sub(r"\s+", " ", stem)[:80].strip(" .") or "Document"
    return f"{stem[:1].upper()}{stem[1:]} (draft).{fmt}"


# ---- comparison --------------------------------------------------------------------------

def compare(template: Mapping[str, Any], made: Sequence[Made], hidden: Sequence[Unit]
            ) -> dict[str, Any]:
    """How close a generated document is to the one the person sent (held out): shapes
    shared (structure), fixed wording found, values written the template's way, tier
    breaks and sign-off the same, and the share of their values the draft filled."""
    got = [m for m in made if m.status != "removed"]
    got_shapes = {shape(m.text) for m in got if m.text and m.text != BLANK and _wordy(m.text)}
    want_shapes = {shape(u.text) for u in hidden if _wordy(u.text)}
    both = got_shapes & want_shapes
    fixed = [squash(m.text) for m in got if m.status == "kept" and _wordy(m.text)]
    hidden_text = Counter(squash(u.text) for u in hidden)
    a = " ".join(shape(m.text) for m in got if m.text != BLANK)
    b = " ".join(shape(u.text) for u in hidden)
    conv = template.get("conventions") or {}
    money = [v for u in hidden for v in values_in(u.text) if v.type == "money"]
    dates = [v for u in hidden for v in values_in(u.text) if v.type == "date"]

    def money_ok(v: Value) -> bool:
        places = len(v.raw.split(".")[1].split()[0].split("/")[0]) if "." in v.raw else 0
        return (v.sign == conv.get("sign", v.sign) and (v.code or "") in ("", conv.get("code", ""))
                and places == conv.get("decimals", places))
    hidden_tiers = [t["tiers"] for t in tables_of(hidden) if t["tiers"]]
    usual = (template.get("tiers") or {}).get("usual")
    sign_hidden = [squash(u.text) for u in hidden[-len(template.get("signoff") or []):]] \
        if template.get("signoff") else []
    sign_made = [squash(m.text) for m in got if m.uid in set(template.get("signoff") or [])]
    want_values = sum(1 for u in hidden for v in values_in(u.text)
                      if v.type in ("money", "qty", "date", "id", "email"))
    return {
        "structure": round(len(both) / len(want_shapes), 4) if want_shapes else None,
        "structure_precision": round(len(both) / len(got_shapes), 4) if got_shapes else None,
        "wording": round(difflib.SequenceMatcher(None, a, b, autojunk=False).ratio(), 4)
        if a and b else 0.0,
        "fixed_found": round(sum(1 for f in fixed if hidden_text[f]) / len(fixed), 4)
        if fixed else None,
        "money_format": round(sum(map(money_ok, money)) / len(money), 4) if money else None,
        "date_format": round(sum(1 for v in dates if v.date_format == conv.get("date_format"))
                             / len(dates), 4) if dates and conv.get("date_format") else None,
        "tiers_same": (bool(hidden_tiers) and hidden_tiers[0] == usual) if (
            hidden_tiers or usual) else None,
        "signoff_same": sign_made == sign_hidden if sign_hidden else None,
        "filled": sum(1 for m in got if m.status in ("filled", "dated")),
        "blank": sum(1 for m in got if m.status == "blank"),
        "values_sent": want_values,
    }


__all__ = ["BLANK", "Generated", "Made", "Member", "Reading", "SCHEMA", "StyleRefused", "Unit",
           "Value", "choose", "cluster", "compare", "conventions", "fingerprint",
           "format_date", "format_money", "generate", "kind_of", "learn", "learn_all", "load",
           "load_kinds", "made_name", "plain_docx", "read", "save", "shape", "similarity",
           "tables_of", "values_in"]
