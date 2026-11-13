"""Fill an existing form (Word, Excel, PDF) from ontology facts, each value with its source.

A person hands the Workbench a document they would otherwise fill by hand (a supplier or
customer questionnaire, a certificate of analysis, a spec sheet, a credit application,
shipping or customs paperwork) and names the record it is about ("for PO 1234"). This
module is the pure part:

* **Fields** (``open_document``): the blanks the document holds, by kind, decided from the
  bytes, never the name. ``.docx``: content controls, legacy text form fields, a table cell
  beside or under a label, and ``Label: ____`` blanks in a paragraph. ``.xlsx``: the cell
  right of or under a label (a label ends with ``:`` or is a known label). A PDF with an
  AcroForm: its text fields (the tooltip, else the name, is the label). A flat PDF:
  ``Label: ____`` blanks on its text layer, filled by an overlay (``pypdf`` and
  ``reportlab``; without them a flat PDF is refused). Anything else is refused.
* **Facts** (``facts_from``): the root record's property values as the reader's own
  ``plx.object`` reads them (``operating.typed_graph.GraphRepository``: the read policy is
  applied there, nothing here widens it), and those of the records one declared to-one link
  away (``doc_fill_terms.json`` ``hops``). Every fact keeps its record, property and source
  handle. A link reaching two records, or values of one property that disagree, makes the
  facts it would give ambiguous, never one picked.
* **Plan** (``plan``): a blank is filled only when its normalized label is one the terms
  file lists and exactly one distinct value stands among the properties that field may be
  read from. Otherwise it stays blank and is flagged: ``unknown_field`` (no rule for the
  label), ``no_fact`` (nothing read), ``ambiguous`` (two values, a disputed property or a
  link reaching two records), ``person_only`` (a signature, who signs, the day it is
  signed), ``kept`` (it already holds a value: never overwritten).
* **Output** (``fill``): new bytes; the original is never changed. Its digest and the
  filled file's are returned with every field's before, after, status and citation, which
  the app shows as the review before anything is saved.

Nothing here reads a source, sends or writes. Evidence label of tests: ``isolated-fixture``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import datetime as dt
from decimal import Decimal
import hashlib
import io
import json
from pathlib import Path
import re
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape
import zipfile

TERMS_FILE = Path(__file__).with_name("doc_fill_terms.json")
TERMS_SCHEMA = "plx-doc-fill-terms/v1"
KINDS = ("docx", "xlsx", "pdf_form", "pdf_flat")
STATUSES = ("filled", "kept", "person_only", "unknown_field", "no_fact", "ambiguous")
MAX_BYTES = 8 * 1024 * 1024
MAX_UNZIPPED = 64 * 1024 * 1024
MAX_FIELDS = 400
MAX_LABEL = 80
MAX_VALUE = 300

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
W14 = "http://schemas.microsoft.com/office/word/2010/wordml"
S = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_W = f"{{{W}}}"
_S = f"{{{S}}}"
_BLANKS = re.compile(r"_{3,}")
_CELL = re.compile(r"^([A-Z]{1,3})(\d{1,7})$")


class FillRefused(ValueError):
    """The document cannot be filled: ``code`` says why (the API words it)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# ---- terms -------------------------------------------------------------------------------

def normalize(label: Any) -> str:
    """A label as matched: lower case, no parenthetical, apostrophe, ``:``, ``*`` or dots,
    ``#`` its own word, single spaces."""
    text = str(label or "").lower()
    text = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", text)
    text = text.replace("’", "").replace("'", "").replace("#", " # ")
    text = re.sub(r"[.:*_ \t\r\n]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" -/")
    return text


@dataclass(frozen=True)
class Terms:
    fields: Mapping[str, tuple[str, tuple[str, ...]]]    # label -> (field, properties)
    person_only: frozenset[str]
    hops: Mapping[str, tuple[tuple[str, str, str], ...]]

    def known(self, label: str) -> bool:
        key = normalize(label)
        return key in self.fields or key in self.person_only


def load_terms(path: Optional[Path] = None) -> Terms:
    """The terms file (``TERMS_FILE``); a file this code does not understand fills nothing
    (``FillRefused("terms_invalid")``)."""
    try:
        raw = json.loads(Path(path or TERMS_FILE).read_text(encoding="utf-8"))
        if raw.get("schema") != TERMS_SCHEMA:
            raise ValueError("schema")
        fields: dict[str, tuple[str, tuple[str, ...]]] = {}
        for entry in raw["fields"]:
            props = tuple(str(p) for p in entry["from"])
            if not props or any(p.count(".") != 1 for p in props):
                raise ValueError("from")
            for label in entry["labels"]:
                key = normalize(label)
                if key in fields and fields[key][0] != entry["field"]:
                    raise ValueError(f"label {key} names two fields")
                fields[key] = (str(entry["field"]), props)
        person = frozenset(normalize(p) for p in raw["person_only"])
        if person & set(fields):
            raise ValueError("a label is both person-only and filled")
        hops = {str(k): tuple((str(a), str(b), str(c)) for a, b, c in v)
                for k, v in raw.get("hops", {}).items()}
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise FillRefused("terms_invalid") from error
    return Terms(fields, person, hops)


# ---- facts -------------------------------------------------------------------------------

@dataclass(frozen=True)
class Citation:
    record: str          # the object id
    field: str           # the property
    source: str          # the L1 handle the value was read from

    def as_dict(self) -> dict[str, str]:
        return {"record": self.record, "field": self.field, "source": self.source}


@dataclass(frozen=True)
class Fact:
    """One property value of one record, as text, with where it was read. ``key``:
    ``<ObjectType>.<property>``. ``ambiguous``: the value may not be used (a disputed
    property, or the record is one of two a to-one link reached)."""
    key: str
    value: str
    citation: Citation
    ambiguous: bool = False


def format_value(value: Any) -> Optional[str]:
    """A typed ontology value as a form takes it; None when it has no plain text form
    (money without its currency, an empty value)."""
    from operating.typed_graph.runtime import Money, Quantity
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Money):
        if not value.currency:
            return None
        return f"{_number(value.amount)} {value.currency}"
    if isinstance(value, Quantity):
        return f"{_number(value.amount)} {value.unit}" if value.unit else _number(value.amount)
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()[:10]
    if isinstance(value, Decimal):
        return _number(value)
    text = " ".join(str(value).split())
    return text[:MAX_VALUE] if text else None


def _number(amount: Decimal) -> str:
    text = format(amount, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _facts_of(obj: Any, ambiguous: bool = False) -> list[Fact]:
    out = []
    for prop, cited in (obj.values or {}).items():
        disputed = ambiguous or prop in obj.conflicts
        for c in cited:
            text = format_value(c.value) if c.problem is None else None
            if text is None or not c.citation.l1_handle:
                continue
            out.append(Fact(f"{obj.OBJECT_TYPE}.{prop}", text,
                            Citation(obj.object_id, prop, str(c.citation.l1_handle)),
                            disputed))
    return out


def facts_from(repo: Any, object_id: str, terms: Terms) -> tuple[list[Fact], list[str], Any]:
    """The facts the reader's repository (``GraphRepository``) gives for ``object_id`` and
    the records one declared hop away, the notes on what could not be read, and the record.
    KeyError: the record is not one the reader may read (or does not exist)."""
    from operating.typed_graph.runtime import CardinalityError, GraphReadError, LinkSpec
    root = repo.get(object_id)
    if root is None:
        raise KeyError("object_not_found")
    facts = _facts_of(root)
    notes: list[str] = []
    for link, direction, target in terms.hops.get(root.OBJECT_TYPE, ()):
        spec = LinkSpec(link, direction, target, True)
        try:
            other = repo.one(root, spec)
        except CardinalityError as two:
            notes.append(f"{link} reaches {len(two.found)} records: none used")
            for oid in two.found[:3]:
                try:
                    got = repo.get(oid)
                except GraphReadError:
                    got = None
                if got is not None:
                    facts += _facts_of(got, ambiguous=True)
            # a field the hop would fill is ambiguous even when no record could be read
            facts.append(Fact(f"{target}.*", "", Citation(object_id, link, ""), True))
            continue
        except GraphReadError as error:
            notes.append(f"{link} not read ({error.code})")
            continue
        if other is not None:
            facts += _facts_of(other)
    return facts, notes, root


# ---- plan --------------------------------------------------------------------------------

@dataclass(frozen=True)
class FormField:
    field_id: str
    label: str
    current: str = ""
    kind: str = "text"            # text | choice


@dataclass(frozen=True)
class Filled:
    field_id: str
    label: str
    before: str
    after: str
    status: str
    field: Optional[str] = None           # the terms file's field name
    citation: Optional[Citation] = None
    also: tuple[Citation, ...] = ()       # further sources of the same value
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"field_id": self.field_id, "label": self.label, "before": self.before,
                "after": self.after, "status": self.status, "field": self.field,
                "citation": self.citation.as_dict() if self.citation else None,
                "also": [c.as_dict() for c in self.also], "reason": self.reason}


# any label about signing or initialling is the signer's, whatever else it says
_SIGNING = re.compile(r"\b(?:sign|signs|signed|signing|signature|signatures|signatory|"
                      r"initial|initials|initialed|initialled|countersign\w*)\b")


def _same(value: str) -> str:
    return re.sub(r"[\s,]+", " ", value).strip().lower()


_SYSTEM_PREFIX = re.compile(r"^(?:bc|fm|sage|portal|silver|erp)_")


def _words(name: str) -> str:
    """A type or property name as words: ``PurchaseOrder`` -> ``purchase order``,
    ``bc_phone_number`` -> ``phone number``."""
    name = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name)
    return normalize(_SYSTEM_PREFIX.sub("", name.lower()).replace("_", " "))


def _schema_rule(key: str, facts: Sequence[Fact]) -> Optional[tuple[str, tuple[str, ...]]]:
    """A label the terms file does not list, matched to the facts read through the schema's
    own names: ``<type> <property>`` (``supplier lead time days``) or a property of two or
    more words (``hs code``, ``fill weight``). A one-word property alone (``name``,
    ``email``, ``status``) names nothing: a form asks for the filler's own there as often as
    for the record's. Every property the label names takes part, so two values disagree."""
    props: set[str] = set()
    for fact in facts:
        kind, _, prop = fact.key.partition(".")
        if prop == "*":
            continue
        words = _words(prop)
        if key == f"{_words(kind)} {words}" or (" " in words and key == words):
            props.add(fact.key)
    return (key, tuple(sorted(props))) if props else None


def plan(fields: Sequence[FormField], facts: Sequence[Fact], terms: Terms) -> list[Filled]:
    """Each field's outcome (module doc, **Plan**)."""
    out = []
    for f in fields:
        key = normalize(f.label)
        if f.current.strip():
            out.append(Filled(f.field_id, f.label, f.current, f.current, "kept",
                              reason="already filled in"))
            continue
        if key in terms.person_only or _SIGNING.search(key):
            out.append(Filled(f.field_id, f.label, "", "", "person_only",
                              reason="for the person who signs"))
            continue
        rule = terms.fields.get(key) or _schema_rule(key, facts)
        if rule is None or f.kind != "text":
            out.append(Filled(f.field_id, f.label, "", "", "unknown_field",
                              reason="no rule for this label" if rule is None
                              else "a choice is left to you"))
            continue
        name, props = rule
        types = {p.split(".", 1)[0] for p in props}
        found = [x for x in facts if x.key in props or (x.key.endswith(".*")
                                                        and x.key[:-2] in types)]
        if any(x.ambiguous for x in found):
            out.append(Filled(f.field_id, f.label, "", "", "ambiguous", name,
                              reason="the records disagree or a link reaches two records"))
            continue
        values: dict[str, list[Fact]] = {}
        for x in found:
            values.setdefault(_same(x.value), []).append(x)
        if not values:
            out.append(Filled(f.field_id, f.label, "", "", "no_fact", name,
                              reason="no record read holds it"))
        elif len(values) > 1:
            out.append(Filled(f.field_id, f.label, "", "", "ambiguous", name,
                              reason=f"{len(values)} different values"))
        else:
            got = next(iter(values.values()))
            out.append(Filled(f.field_id, f.label, "", got[0].value, "filled", name,
                              got[0].citation, tuple(x.citation for x in got[1:5])))
    return out


# ---- documents ---------------------------------------------------------------------------

def detect(data: bytes) -> str:
    """``docx``, ``xlsx`` or ``pdf`` from the bytes; FillRefused otherwise."""
    if not isinstance(data, (bytes, bytearray)) or not data:
        raise FillRefused("empty")
    if len(data) > MAX_BYTES:
        raise FillRefused("too_large")
    if data[:5] == b"%PDF-":
        return "pdf"
    if data[:4] == b"PK\x03\x04":
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                names = set(z.namelist())
                if sum(i.file_size for i in z.infolist()) > MAX_UNZIPPED:
                    raise FillRefused("too_large")
        except zipfile.BadZipFile:
            raise FillRefused("not_a_form") from None
        if "word/document.xml" in names:
            return "docx"
        if "xl/workbook.xml" in names:
            return "xlsx"
    raise FillRefused("not_a_form")


class _Doc:
    kind = ""

    def fields(self) -> list[FormField]:
        raise NotImplementedError

    def write(self, values: Mapping[str, str]) -> bytes:
        raise NotImplementedError


def _label_like(text: str, terms: Terms) -> bool:
    t = text.strip()
    if not t or len(t) > MAX_LABEL or _BLANKS.search(t):
        return False
    return t.endswith(":") or terms.known(t)


def _rezip(original: bytes, replaced: Mapping[str, bytes]) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(original)) as src, \
            zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for info in src.infolist():
            dst.writestr(info, replaced.get(info.filename, src.read(info.filename)))
    return out.getvalue()


# -- docx

def _register(raw: bytes) -> None:
    for prefix, uri in re.findall(rb'xmlns:([A-Za-z_][\w.-]*)="([^"]+)"', raw[:20000]):
        try:
            ET.register_namespace(prefix.decode(), uri.decode())
        except ValueError:
            pass


def _root_tag(raw: bytes) -> tuple[bytes, bytes]:
    """The XML declaration and the root element's start tag, as written."""
    decl = re.match(rb"\s*(<\?xml[^>]*\?>)", raw)
    start = re.search(rb"<(?![?!])[^>]+>", raw)
    return (decl.group(1) if decl else b""), (start.group(0) if start else b"")


def _serialize(root: ET.Element, raw: bytes) -> bytes:
    """``root`` written back with the original's declaration and root start tag (its every
    namespace declaration and ``mc:Ignorable``, which a re-serialization would drop)."""
    decl, start = _root_tag(raw)
    body = ET.tostring(root, encoding="utf-8", xml_declaration=False)
    first = re.match(rb"<[^>]+>", body)
    if first is None or first.group(0).endswith(b"/>") or start.endswith(b"/>"):
        raise FillRefused("not_a_form")
    return decl + start + body[first.end():]


def _text(el: ET.Element) -> str:
    return "".join(t.text or "" for t in el.iter(_W + "t"))


def _set_text(container: ET.Element, value: str, style_from: Optional[ET.Element] = None
              ) -> None:
    """``value`` as the container's only text: in its first text node, or a new run in its
    first paragraph (styled like ``style_from``'s first run)."""
    ts = list(container.iter(_W + "t"))
    if ts:
        ts[0].text = value
        ts[0].set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
        for t in ts[1:]:
            t.text = ""
        return
    para = container if container.tag == _W + "p" else next(container.iter(_W + "p"), None)
    if para is None:
        para = ET.SubElement(container, _W + "p")
    run = ET.SubElement(para, _W + "r")
    src = next(style_from.iter(_W + "rPr"), None) if style_from is not None else None
    if src is not None:
        run.append(ET.fromstring(ET.tostring(src)))
    t = ET.SubElement(run, _W + "t")
    t.text = value
    t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")


class _Docx(_Doc):
    kind = "docx"

    def __init__(self, data: bytes, terms: Terms) -> None:
        self.blob = data
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            self.raw = z.read("word/document.xml")
        _register(self.raw)
        try:
            self.root = ET.fromstring(self.raw)
        except ET.ParseError:
            raise FillRefused("not_a_form") from None
        self.terms = terms
        self.setters: dict[str, Callable[[str], None]] = {}
        self._fields: list[FormField] = []
        self._read()

    def _add(self, fid: str, label: str, current: str, setter: Callable[[str], None],
             kind: str = "text") -> None:
        if len(self._fields) < MAX_FIELDS:
            self._fields.append(FormField(fid, " ".join(label.split())[:MAX_LABEL],
                                          " ".join(current.split()), kind))
            self.setters[fid] = setter

    def _read(self) -> None:
        taken: set[int] = set()
        for n, sdt in enumerate(self.root.iter(_W + "sdt")):
            pr = sdt.find(_W + "sdtPr")
            content = sdt.find(_W + "sdtContent")
            if pr is None or content is None:
                continue
            name = next((e.get(_W + "val") for tag in ("alias", "tag")
                         for e in [pr.find(_W + tag)] if e is not None and e.get(_W + "val")),
                        None)
            if not name:
                continue
            choice = pr.find(f"{{{W14}}}checkbox") is not None or pr.find(
                _W + "dropDownList") is not None or pr.find(_W + "comboBox") is not None
            placeholder = pr.find(_W + "showingPlcHdr") is not None
            current = "" if placeholder else _text(content)

            def setter(value: str, pr: ET.Element = pr, content: ET.Element = content) -> None:
                hold = pr.find(_W + "showingPlcHdr")
                if hold is not None:
                    pr.remove(hold)
                _set_text(content, value)
            self._add(f"docx:sdt:{n}", name, current, setter, "choice" if choice else "text")
            taken.update(id(p) for p in content.iter(_W + "p"))
        for t_n, table in enumerate(self.root.iter(_W + "tbl")):
            rows = [[c for c in tr.findall(_W + "tc")] for tr in table.findall(_W + "tr")]
            for r, cells in enumerate(rows):
                texts = [_text(c).strip() for c in cells]
                below = rows[r + 1] if r + 1 < len(rows) else None
                header = [t for t in texts if t]
                if len(header) > 1 and below is not None and len(below) == len(cells) and all(
                        _label_like(t, self.terms) or not t for t in texts) and not any(
                        _label_like(_text(c), self.terms) for c in below):
                    for c, (label, cell) in enumerate(zip(texts, below)):
                        if label:
                            self._cell(f"docx:t{t_n}:r{r + 1}:c{c}", label, cell, cells[c],
                                       taken)
                    continue
                for c in range(len(cells) - 1):
                    if _label_like(texts[c], self.terms) and id(cells[c + 1]) not in taken \
                            and not _label_like(texts[c + 1], self.terms):
                        self._cell(f"docx:t{t_n}:r{r}:c{c + 1}", texts[c], cells[c + 1],
                                   cells[c], taken)
        for p_n, para in enumerate(self.root.iter(_W + "p")):
            if id(para) in taken:
                continue
            self._legacy(p_n, para)
            self._blanks(p_n, para)

    def _cell(self, fid: str, label: str, cell: ET.Element, label_cell: ET.Element,
              taken: set[int]) -> None:
        if id(cell) in taken:
            return
        taken.add(id(cell))
        taken.update(id(p) for p in cell.iter(_W + "p"))
        current = _text(cell)
        self._add(fid, label.rstrip(": "), "" if _blank(current) else current,
                  lambda v, cell=cell, lab=label_cell: _set_text(cell, v, lab))

    def _legacy(self, p_n: int, para: ET.Element) -> None:
        """Legacy text form fields (``FORMTEXT``): the runs between ``separate`` and
        ``end`` hold the value; the label is the text before the field (else its name)."""
        runs = list(para.iter(_W + "r"))
        before: list[str] = []
        i = 0
        while i < len(runs):
            fc = runs[i].find(_W + "fldChar")
            ff = fc.find(_W + "ffData") if fc is not None else None
            if fc is not None and fc.get(_W + "fldCharType") == "begin" and ff is not None \
                    and ff.find(_W + "textInput") is not None:
                name_el = ff.find(_W + "name")
                value_runs: list[ET.Element] = []
                j, seen_sep = i + 1, False
                while j < len(runs):
                    c = runs[j].find(_W + "fldChar")
                    if c is not None and c.get(_W + "fldCharType") == "separate":
                        seen_sep = True
                    elif c is not None and c.get(_W + "fldCharType") == "end":
                        break
                    elif seen_sep:
                        value_runs.append(runs[j])
                    j += 1
                label = "".join(before).strip().rstrip(":").strip() or (
                    name_el.get(_W + "val") if name_el is not None else "")
                label = label.split("  ")[-1] if label else ""
                current = "".join(_text(r) for r in value_runs).replace(" ", "").strip()
                if label and value_runs:
                    def setter(value: str, runs: list[ET.Element] = value_runs) -> None:
                        ts = [t for r in runs for t in r.iter(_W + "t")]
                        if ts:
                            ts[0].text = value
                            for t in ts[1:]:
                                t.text = ""
                    self._add(f"docx:ff:{p_n}:{i}", label, current, setter)
                before = []
                i = j + 1
                continue
            before.append(_text(runs[i]))
            i += 1

    def _blanks(self, p_n: int, para: ET.Element) -> None:
        """``Label: ____`` blanks: the label is the text since the previous blank."""
        ts = list(para.iter(_W + "t"))
        if not any(_BLANKS.search(t.text or "") for t in ts):
            return
        since = ""
        for t_n, t in enumerate(ts):
            text = t.text or ""
            pos = 0
            for k, m in enumerate(_BLANKS.finditer(text)):
                since += text[pos:m.start()]
                label = since.strip().rstrip(":").strip()
                if label and len(label) <= MAX_LABEL:
                    self._add(f"docx:b:{p_n}:{t_n}:{k}", label, "",
                              lambda v, t=t, k=k: _replace_blank(t, k, v))
                since, pos = "", m.end()
            since += text[pos:]

    def fields(self) -> list[FormField]:
        return list(self._fields)

    def write(self, values: Mapping[str, str]) -> bytes:
        # blanks in one text node are replaced last first, so earlier indexes still hold
        for fid in sorted(values, key=lambda f: (f.startswith("docx:b:"), f), reverse=True):
            self.setters[fid](values[fid])
        return _rezip(self.blob, {"word/document.xml": _serialize(self.root, self.raw)})


def _blank(text: str) -> bool:
    return not text.replace("_", "").strip()


def _replace_blank(t: ET.Element, k: int, value: str) -> None:
    spans = list(_BLANKS.finditer(t.text or ""))
    m = spans[k]
    t.text = (t.text or "")[:m.start()] + value + (t.text or "")[m.end():]
    t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")


# -- xlsx

def _col(letters: str) -> int:
    n = 0
    for ch in letters:
        n = n * 26 + ord(ch) - 64
    return n


def _letters(n: int) -> str:
    out = ""
    while n:
        n, r = divmod(n - 1, 26)
        out = chr(65 + r) + out
    return out


class _Xlsx(_Doc):
    """Cells are read with ElementTree; a fill is a string edit of the sheet's XML (the
    cell element replaced or inserted in column order), so everything else in the part is
    byte for byte as it was."""
    kind = "xlsx"

    def __init__(self, data: bytes, terms: Terms) -> None:
        self.blob, self.terms = data, terms
        self._fields: list[FormField] = []
        self.where: dict[str, tuple[str, str]] = {}      # field id -> (part, cell ref)
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names = set(z.namelist())
            shared = self._shared(z.read("xl/sharedStrings.xml")) \
                if "xl/sharedStrings.xml" in names else []
            self.sheets = self._sheets(z, names)
            self.parts = {part: z.read(part) for _name, part in self.sheets}
        for name, part in self.sheets:
            self._read(name, part, shared)

    @staticmethod
    def _shared(raw: bytes) -> list[str]:
        root = ET.fromstring(raw)
        return ["".join(t.text or "" for t in si.iter(_S + "t")) for si in root.findall(_S + "si")]

    @staticmethod
    def _sheets(z: zipfile.ZipFile, names: set[str]) -> list[tuple[str, str]]:
        book = ET.fromstring(z.read("xl/workbook.xml"))
        rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels")) \
            if "xl/_rels/workbook.xml.rels" in names else None
        targets = {r.get("Id"): r.get("Target") for r in (rels if rels is not None else ())}
        out = []
        for sheet in book.iter(_S + "sheet"):
            target = targets.get(sheet.get(f"{{{R_NS}}}id")) or ""
            part = target.lstrip("/") if target.startswith("/") else "xl/" + target
            if part in names:
                out.append((sheet.get("name") or part, part))
        return out

    def _read(self, name: str, part: str, shared: list[str]) -> None:
        try:
            root = ET.fromstring(self.parts[part])
        except ET.ParseError:
            raise FillRefused("not_a_form") from None
        cells: dict[tuple[int, int], str] = {}
        for c in root.iter(_S + "c"):
            m = _CELL.match(c.get("r") or "")
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
            if not text and c.find(_S + "f") is not None:
                text = "="              # a formula with no cached value is not a blank
            cells[(int(m.group(2)), _col(m.group(1)))] = text
        merged: dict[tuple[int, int], int] = {}     # a merged label's last column
        for mc in root.iter(_S + "mergeCell"):
            a, _, b = (mc.get("ref") or "").partition(":")
            ma, mb = _CELL.match(a), _CELL.match(b)
            if ma and mb:
                merged[(int(ma.group(2)), _col(ma.group(1)))] = _col(mb.group(1))
                for r in range(int(ma.group(2)), int(mb.group(2)) + 1):
                    for c in range(_col(ma.group(1)), _col(mb.group(1)) + 1):
                        if (r, c) != (int(ma.group(2)), _col(ma.group(1))):
                            merged.setdefault((r, c), -1)        # covered by a merge
        used: set[tuple[int, int]] = set()
        for (r, c), text in sorted(cells.items()):
            if not _label_like(text, self.terms):
                continue
            end = merged.get((r, c), c)
            right = (r, (end if end > 0 else c) + 1)
            beside = cells.get(right, "")
            if _label_like(beside, self.terms) or _label_like(cells.get((r, c - 1), ""),
                                                              self.terms):
                # a header row (labels side by side): the blank under the label
                below = (r + 1, c)
                if below not in used and merged.get(below) != -1:
                    current = cells.get(below, "")
                    self._add(name, part, below, text, "" if _blank(current) else current)
                    used.add(below)
            elif right not in used and merged.get(right) != -1:
                self._add(name, part, right, text, "" if _blank(beside) else beside)
                used.add(right)

    def _add(self, sheet: str, part: str, spot: tuple[int, int], label: str, current: str
             ) -> None:
        if len(self._fields) >= MAX_FIELDS:
            return
        ref = f"{_letters(spot[1])}{spot[0]}"
        fid = f"xlsx:{sheet}!{ref}"
        self._fields.append(FormField(fid, " ".join(label.split()).rstrip(": ")[:MAX_LABEL],
                                      " ".join(current.split())))
        self.where[fid] = (part, ref)

    def fields(self) -> list[FormField]:
        return list(self._fields)

    def write(self, values: Mapping[str, str]) -> bytes:
        parts = dict(self.parts)
        for fid, value in values.items():
            part, ref = self.where[fid]
            parts[part] = _put_cell(parts[part], ref, value)
        return _rezip(self.blob, {p: parts[p] for p in parts if parts[p] is not self.parts[p]})


def _put_cell(xml: bytes, ref: str, value: str) -> bytes:
    """The sheet XML with cell ``ref`` holding ``value`` as an inline string (its style
    kept when the cell exists)."""
    m = _CELL.match(ref)
    row, col = int(m.group(2)), _col(m.group(1))
    text = escape(value).encode("utf-8")

    def cell(style: bytes) -> bytes:
        return (b'<c r="' + ref.encode() + b'"' + style + b' t="inlineStr"><is><t xml:space='
                b'"preserve">' + text + b"</t></is></c>")
    row_re = re.compile(rb'<row\b[^>]*\br="' + str(row).encode() + rb'"[^>]*?(?:/>|>.*?</row>)',
                        re.S)
    found = row_re.search(xml)
    if found is None:
        data = re.search(rb"<sheetData\s*/>|<sheetData[^>]*>", xml)
        if data is None:
            raise FillRefused("not_a_form")
        if data.group(0).endswith(b"/>"):
            return xml[:data.start()] + b"<sheetData><row r=\"" + str(row).encode() + \
                b"\">" + cell(b"") + b"</row></sheetData>" + xml[data.end():]
        later = None
        for other in re.finditer(rb'<row\b[^>]*\br="(\d+)"', xml[data.end():]):
            if int(other.group(1)) > row:
                later = data.end() + other.start()
                break
        at = later if later is not None else xml.index(b"</sheetData>", data.end())
        return xml[:at] + b'<row r="' + str(row).encode() + b'">' + cell(b"") + b"</row>" + \
            xml[at:]
    whole = found.group(0)
    if whole.endswith(b"/>"):
        new = whole[:-2] + b">" + cell(b"") + b"</row>"
        return xml[:found.start()] + new + xml[found.end():]
    head_end = whole.index(b">") + 1
    body = whole[head_end:-len(b"</row>")]
    insert_at = len(body)
    for c in re.finditer(rb'<c\b[^>]*?(?:/>|>.*?</c>)', body, re.S):
        r = re.search(rb'\br="([A-Z]+)(\d+)"', c.group(0))
        if r is None:
            continue
        here = _col(r.group(1).decode())
        if here == col:
            style = re.search(rb'\bs="\d+"', c.group(0))
            body = body[:c.start()] + cell(b" " + style.group(0) if style else b"") + \
                body[c.end():]
            break
        if here > col:
            insert_at = c.start()
            body = body[:insert_at] + cell(b"") + body[insert_at:]
            break
    else:
        body = body + cell(b"")
    return xml[:found.start()] + whole[:head_end] + body + b"</row>" + xml[found.end():]


# -- pdf

class _PdfForm(_Doc):
    kind = "pdf_form"

    def __init__(self, reader: Any) -> None:
        self.reader = reader
        self._fields: list[FormField] = []
        self.names: dict[str, str] = {}
        for name, f in (reader.get_fields() or {}).items():
            if len(self._fields) >= MAX_FIELDS:
                break
            kind = f.get("/FT")
            label = str(f.get("/TU") or f.get("/T") or name)
            value = f.get("/V")
            current = "" if value is None or str(value) in ("/Off", "Off") else str(value)
            fid = f"pdf:{name}"
            self._fields.append(FormField(fid, " ".join(label.split())[:MAX_LABEL],
                                          " ".join(current.split()),
                                          "text" if kind == "/Tx" else "choice"))
            self.names[fid] = name

    def fields(self) -> list[FormField]:
        return list(self._fields)

    def write(self, values: Mapping[str, str]) -> bytes:
        from pypdf import PdfWriter
        writer = PdfWriter(clone_from=self.reader)
        writer.update_page_form_field_values(
            None, {self.names[f]: v for f, v in values.items()}, auto_regenerate=False)
        writer.set_need_appearances_writer(True)
        out = io.BytesIO()
        writer.write(out)
        return out.getvalue()


class _PdfFlat(_Doc):
    """``Label: ____`` blanks on a PDF's text layer; the value is drawn over the blank's
    start on a new layer (``reportlab``), the page's own content untouched."""
    kind = "pdf_flat"

    def __init__(self, reader: Any) -> None:
        self.reader = reader
        self._fields: list[FormField] = []
        self.spots: dict[str, tuple[int, float, float, float]] = {}
        for p_n, page in enumerate(reader.pages[:20]):
            runs: list[tuple[str, float, float, float]] = []

            def visit(text: str, cm: Any, tm: Any, _font: Any, size: float,
                      runs: list = runs) -> None:
                if text and text.strip():
                    x = tm[4] * cm[0] + tm[5] * cm[2] + cm[4]
                    y = tm[4] * cm[1] + tm[5] * cm[3] + cm[5]
                    runs.append((text, x, y, float(size or 10) * abs(tm[0] or 1)))
            try:
                page.extract_text(visitor_text=visit)
            except Exception:  # noqa: BLE001 - an unreadable text layer: no field there
                continue
            for k, (text, x, y, size) in enumerate(runs):
                since = 0
                for b_n, m in enumerate(_BLANKS.finditer(text)):
                    label = text[since:m.start()].strip().rstrip(":").strip()
                    if not label and k and m.start() == 0:
                        prev = runs[k - 1]
                        label = prev[0].strip().rstrip(":").strip() \
                            if abs(prev[2] - y) < 2 else ""
                    since = m.end()
                    if not label or len(label) > MAX_LABEL:
                        continue
                    fid = f"pdfflat:{p_n}:{k}:{b_n}"
                    # where the blank starts: the run's x plus the text before it (an
                    # average glyph is half the font size wide)
                    self.spots[fid] = (p_n, x + 0.5 * size * m.start(), y + 1.5, size)
                    self._fields.append(FormField(fid, " ".join(label.split())[:MAX_LABEL]))

    def fields(self) -> list[FormField]:
        return list(self._fields)

    def write(self, values: Mapping[str, str]) -> bytes:
        try:
            from reportlab.pdfgen import canvas
        except ImportError:
            raise FillRefused("pdf_overlay_unavailable") from None
        from pypdf import PdfReader, PdfWriter
        writer = PdfWriter(clone_from=self.reader)
        by_page: dict[int, list[tuple[float, float, float, str]]] = {}
        for fid, value in values.items():
            p_n, x, y, size = self.spots[fid]
            by_page.setdefault(p_n, []).append((x, y, size, value))
        for p_n, items in by_page.items():
            page = writer.pages[p_n]
            box = page.mediabox
            buf = io.BytesIO()
            pdf = canvas.Canvas(buf, pagesize=(float(box.width), float(box.height)),
                                invariant=1)
            for x, y, size, value in items:
                pdf.setFont("Helvetica", max(6.0, min(size, 12.0)))
                pdf.drawString(x, y, value)
            pdf.save()
            page.merge_page(PdfReader(io.BytesIO(buf.getvalue())).pages[0])
        out = io.BytesIO()
        writer.write(out)
        return out.getvalue()


def open_document(data: bytes, terms: Terms) -> _Doc:
    """The document's fields and writer (module doc, **Fields**)."""
    kind = detect(data)
    if kind == "docx":
        return _Docx(data, terms)
    if kind == "xlsx":
        try:
            return _Xlsx(data, terms)
        except (KeyError, ET.ParseError):
            raise FillRefused("not_a_form") from None
    try:
        from pypdf import PdfReader
        from pypdf.errors import PdfReadError
    except ImportError:
        raise FillRefused("pdf_unavailable") from None
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise FillRefused("encrypted")
        form = _PdfForm(reader) if reader.get_fields() else None
    except (PdfReadError, ValueError, KeyError, TypeError):
        raise FillRefused("not_a_form") from None
    if form is not None and form.fields():
        return form
    return _PdfFlat(reader)


# ---- the whole fill ----------------------------------------------------------------------

@dataclass(frozen=True)
class FillResult:
    kind: str
    fields: tuple[Filled, ...]
    data: Optional[bytes]             # the filled copy; None when nothing was filled
    digest_before: str
    digest_after: Optional[str]
    notes: tuple[str, ...] = ()

    def counts(self) -> dict[str, int]:
        out = {s: 0 for s in STATUSES}
        for f in self.fields:
            out[f.status] += 1
        return out


def fill(data: bytes, facts: Sequence[Fact], terms: Terms, notes: Iterable[str] = ()
         ) -> FillResult:
    """``data`` filled from ``facts``: a new file (the original bytes are not touched) and
    every field's outcome. A document with no field is refused (``no_fields``)."""
    doc = open_document(bytes(data), terms)
    fields = doc.fields()
    if not fields:
        raise FillRefused("no_fields")
    planned = plan(fields, facts, terms)
    values = {f.field_id: f.after for f in planned if f.status == "filled"}
    out = doc.write(values) if values else None
    return FillResult(doc.kind, tuple(planned), out, hashlib.sha256(data).hexdigest(),
                      hashlib.sha256(out).hexdigest() if out else None, tuple(notes))


def filled_name(original: str, kind: str) -> str:
    """The new file's name beside the original: ``<stem> (filled).<ext>``, only the
    characters a SharePoint upload takes (``SharePointAdapter.file_name_refused``)."""
    ext = {"docx": "docx", "xlsx": "xlsx"}.get(kind, "pdf")
    stem = re.sub(r"\.(docx|docm|xlsx|xlsm|pdf)$", "", str(original or ""), flags=re.I)
    stem = re.sub(r"[^A-Za-z0-9._() -]+", " ", stem)
    stem = re.sub(r"\s+", " ", stem).strip(" .")[:80].strip() or "Form"
    return f"{stem} (filled).{ext}"


MEDIA = {"docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
         "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
         "pdf_form": "application/pdf", "pdf_flat": "application/pdf"}


__all__ = ["Citation", "Fact", "FillRefused", "FillResult", "Filled", "FormField", "MEDIA",
           "Terms", "detect", "facts_from", "fill", "filled_name", "format_value",
           "load_terms", "normalize", "open_document", "plan"]
