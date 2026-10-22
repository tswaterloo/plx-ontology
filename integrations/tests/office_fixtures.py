"""Synthetic Word originals shared by the document-conversion and mail-capture tests (stdlib only).

A helper, not a test module: test modules import these fixtures from here rather than from
each other, so a change to one test module selects only that module.
"""

from __future__ import annotations

import io
from xml.sax.saxutils import escape
from zipfile import ZipFile

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
PKG_RELS = "http://schemas.openxmlformats.org/package/2006/relationships"
DOC_RELS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def package_rels(*items: tuple[str, str, str]) -> str:
    return (f"<Relationships xmlns='{PKG_RELS}'>" + "".join(
        f"<Relationship Id='{rid}' Type='{DOC_RELS}/{kind}' Target='{target}'/>"
        for rid, kind, target in items) + "</Relationships>")


def content_types(overrides: dict[str, str]) -> str:
    return ("<Types xmlns='http://schemas.openxmlformats.org/package/2006/content-types'>"
            "<Default Extension='rels' ContentType='application/vnd.openxmlformats-package.relationships+xml'/>"
            "<Default Extension='xml' ContentType='application/xml'/>" + "".join(
                f"<Override PartName='/{name}' ContentType='{kind}'/>"
                for name, kind in overrides.items()) + "</Types>")


def make_word(body: str, *, styles: str = "", levels: str = "") -> bytes:
    """A DOCX with a ``heading 1`` style and a bullet list definition, plus any
    extra ``styles`` and numbering ``levels``."""
    wml = "application/vnd.openxmlformats-officedocument.wordprocessingml"
    buffer = io.BytesIO()
    with ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", content_types({
            "word/document.xml": wml + ".document.main+xml",
            "word/styles.xml": wml + ".styles+xml", "word/numbering.xml": wml + ".numbering+xml"}))
        archive.writestr("_rels/.rels",
                         package_rels(("rId1", "officeDocument", "word/document.xml")))
        archive.writestr("word/_rels/document.xml.rels", package_rels(
            ("rId1", "styles", "styles.xml"), ("rId2", "numbering", "numbering.xml")))
        archive.writestr("word/styles.xml", f"<w:styles xmlns:w='{W}'><w:style w:type='paragraph' "
                         f"w:styleId='Heading1'><w:name w:val='heading 1'/></w:style>{styles}</w:styles>")
        archive.writestr("word/numbering.xml", f"<w:numbering xmlns:w='{W}'><w:abstractNum "
                         "w:abstractNumId='0'><w:lvl w:ilvl='0'><w:numFmt w:val='bullet'/></w:lvl>"
                         f"{levels}"
                         "</w:abstractNum><w:num w:numId='1'><w:abstractNumId w:val='0'/></w:num>"
                         "</w:numbering>")
        archive.writestr("word/document.xml", f"<w:document xmlns:w='{W}'><w:body>{body}"
                         "</w:body></w:document>")
    return buffer.getvalue()


def paragraph(text: str, *, style: str = "", bullet: bool = False, extra: str = "") -> str:
    props = (f"<w:pStyle w:val='{style}'/>" if style else "") + (
        "<w:numPr><w:ilvl w:val='0'/><w:numId w:val='1'/></w:numPr>" if bullet else "")
    return (f"<w:p><w:pPr>{props}</w:pPr><w:r><w:t xml:space='preserve'>{escape(text)}</w:t>"
            f"{extra}</w:r></w:p>")


def _word_table(rows: list[list[str]]) -> str:
    out = "<w:tbl>"
    for index, row in enumerate(rows):
        header = "<w:trPr><w:tblHeader/></w:trPr>" if index == 0 else ""
        cells = "".join(f"<w:tc>{paragraph(cell)}</w:tc>" for cell in row)
        out += f"<w:tr>{header}{cells}</w:tr>"
    return out + "</w:tbl>"


PURCHASE_ORDER_BODY = (paragraph("Purchase Order", style="Heading1")
                       + paragraph("Petra PO: PO_12345") + paragraph("Item: BTL-100", bullet=True)
                       + paragraph("Supplier: Acme", bullet=True)
                       + _word_table([["SKU", "Qty"], ["BTL_100", "100"]]))
PURCHASE_ORDER_MD = ("# Purchase Order\n\nPetra PO: PO\\_12345\n\n* Item: BTL-100\n"
                     "* Supplier: Acme\n\n| SKU | Qty |\n| --- | --- |\n| BTL\\_100 | 100 |")
