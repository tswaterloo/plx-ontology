"""Slides and pages with charts, standard library only: a PowerPoint deck (``pptx``: text
slides and one slide per chart) and the parts a Word document needs to carry a chart
(``docx_chart``). A native chart (``chart_ooxml.NATIVE``) is Office's own chart over an
embedded workbook of its rows ("Edit data" opens it); a heatmap, a coloured table and KPI
tiles are tables coloured by value. Each chart slide or page states where its numbers come
from (``chart_ooxml.provenance``). Evidence: isolated-fixture."""

from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence
from xml.sax.saxutils import escape

from operating import chart_ooxml as ox

NS_P = ox.NS_P
NS_A, NS_R, NS_C = ox.NS_A, ox.NS_R, ox.NS_C
NS_TABLE = "http://schemas.openxmlformats.org/drawingml/2006/table"
PML = "application/vnd.openxmlformats-officedocument.presentationml."
SLIDE_W, SLIDE_H = 12192000, 6858000      # 16:9
MARGIN = 457200
INK, MUTED = "1F2937", "6B7280"
MAX_SLIDES = 60

THEME = (f'{ox.XML}<a:theme xmlns:a="{NS_A}" name="Workbench"><a:themeElements>'
         '<a:clrScheme name="Workbench"><a:dk1><a:srgbClr val="1F2937"/></a:dk1><a:lt1>'
         '<a:srgbClr val="FFFFFF"/></a:lt1><a:dk2><a:srgbClr val="374151"/></a:dk2><a:lt2>'
         '<a:srgbClr val="F3F4F6"/></a:lt2>'
         + "".join(f"<a:accent{i}><a:srgbClr val=\"{c}\"/></a:accent{i}>"
                   for i, c in enumerate(ox.PALETTE[:6], 1))
         + '<a:hlink><a:srgbClr val="2E6FB7"/></a:hlink><a:folHlink><a:srgbClr val="7C5CBF"/>'
         '</a:folHlink></a:clrScheme><a:fontScheme name="Workbench"><a:majorFont><a:latin '
         'typeface="Calibri"/><a:ea typeface=""/><a:cs typeface=""/></a:majorFont><a:minorFont>'
         '<a:latin typeface="Calibri"/><a:ea typeface=""/><a:cs typeface=""/></a:minorFont>'
         '</a:fontScheme><a:fmtScheme name="Workbench"><a:fillStyleLst>'
         + '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill>' * 3
         + "</a:fillStyleLst><a:lnStyleLst>"
         + '<a:ln w="9525"><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:ln>' * 3
         + "</a:lnStyleLst><a:effectStyleLst>"
         + "<a:effectStyle><a:effectLst/></a:effectStyle>" * 3
         + "</a:effectStyleLst><a:bgFillStyleLst>"
         + '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill>' * 3
         + "</a:bgFillStyleLst></a:fmtScheme></a:themeElements></a:theme>")
_TREE = ('<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>'
         '<p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/><a:chOff x="0" y="0"/>'
         '<a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr>')
_NS = f'xmlns:a="{NS_A}" xmlns:r="{NS_R}" xmlns:p="{NS_P}"'


def _run(text: str, size: int, bold: bool = False, color: str = INK) -> str:
    return (f'<a:r><a:rPr lang="en-US" sz="{size}"{" b=\"1\"" if bold else ""} dirty="0">'
            f'<a:solidFill><a:srgbClr val="{color}"/></a:solidFill></a:rPr>'
            f"<a:t>{escape(text)}</a:t></a:r>")


def _text_box(sid: int, name: str, x: int, y: int, w: int, h: int,
              paragraphs: Sequence[str]) -> str:
    return (f'<p:sp><p:nvSpPr><p:cNvPr id="{sid}" name="{name}"/><p:cNvSpPr txBox="1"/>'
            f'<p:nvPr/></p:nvSpPr><p:spPr><a:xfrm><a:off x="{x}" y="{y}"/><a:ext cx="{w}" '
            f'cy="{h}"/></a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom><a:noFill/>'
            '</p:spPr><p:txBody><a:bodyPr wrap="square" lIns="0" tIns="0" rIns="0" bIns="0">'
            '<a:normAutofit/></a:bodyPr><a:lstStyle/>'
            + "".join(paragraphs) + "</p:txBody></p:sp>")


def _para(text: str, size: int, bold: bool = False, color: str = INK,
          bullet: bool = False) -> str:
    ppr = ('<a:pPr marL="228600" indent="-228600"><a:buChar char="&#8226;"/></a:pPr>'
           if bullet else "")
    return f"<a:p>{ppr}{_run(text, size, bold, color)}</a:p>"


def _grid(chart: Mapping[str, Any]) -> tuple[list[str], list[list[str]],
                                              list[list[Optional[str]]]]:
    """A table chart's (header, text rows, fills): KPI tiles as measure / now / change."""
    if chart["kind"] == "kpi":
        header, values, change = [], [], []
        labels = list(chart.get("labels") or ())
        for s in chart.get("series") or ():
            at = [i for i, v in enumerate(s["values"]) if v is not None]
            header.append(s["name"].replace("_", " "))
            values.append(ox.fmt(s["values"][at[-1]]) if at else "")
            if len(at) >= 2:
                now, before = s["values"][at[-1]], s["values"][at[-2]]
                d = now - before
                pct = f" ({d / before:+.0%})" if before else ""
                now_at = labels[at[-1]] if at[-1] < len(labels) else None
                so_far = " so far" if now_at == chart.get("open_period") else ""
                part = " (part)" if labels[at[-2]] == chart.get("first_period") else ""
                said = (f"{now_at}{so_far} vs {labels[at[-2]]}{part}" if now_at is not None
                        else "on the row before")
                change.append(f"{'+' if d >= 0 else ''}{ox.fmt(d)}{pct}, {said}")
            else:
                change.append(labels[at[-1]] if at and at[-1] < len(labels) else "")
        return header, [values, change], [[None] * len(header)] * 2
    header, rows, fills = ox.colour_grid(chart)
    return ([str(h) for h in header], [[ox.fmt(v) for v in r] for r in rows], fills)


def _pptx_table(sid: int, chart: Mapping[str, Any], x: int, y: int, w: int, h: int) -> str:
    header, rows, fills = _grid(chart)
    return table_frame(sid, header, rows, fills, x, y, w, h, big=chart["kind"] == "kpi")


def table_frame(sid: int, header: Sequence[str], rows: Sequence[Sequence[str]],
                fills: Sequence[Sequence[Optional[str]]], x: int, y: int, w: int, h: int,
                *, big: bool = False, right: Sequence[bool] = (),
                widths: Sequence[int] = ()) -> str:
    """A native PowerPoint table: a shaded header row and ``rows`` (up to 40), ``right``:
    the columns aligned right (numbers), ``widths``: relative column widths."""
    rows = list(rows)[:40]
    ncol = max(len(header), 1)
    weights = list(widths) if len(widths) == ncol else [1] * ncol
    col_ws = [w * k // max(sum(weights), 1) for k in weights]
    row_h = min(370840, h // max(len(rows) + 1, 1))

    def cell(text: str, fill: Optional[str], bold: bool = False, size: int = 1100,
             c: int = 0) -> str:
        fill_xml = f'<a:solidFill><a:srgbClr val="{fill}"/></a:solidFill>' if fill else ""
        align = '<a:pPr algn="r"/>' if c < len(right) and right[c] else ""
        return (f'<a:tc><a:txBody><a:bodyPr/><a:lstStyle/><a:p>{align}'
                f"{_run(text, size, bold, ox.ink(fill))}</a:p></a:txBody><a:tcPr>{fill_xml}"
                "</a:tcPr></a:tc>")
    trs = [f'<a:tr h="{row_h}">' + "".join(cell(t, "F3F4F6", True, c=c)
                                           for c, t in enumerate(header)) + "</a:tr>"]
    for r, row in enumerate(rows):
        size = 2800 if big and r == 0 else 1000 if big else 1100
        row = list(row) + [""] * (ncol - len(row))
        trs.append(f'<a:tr h="{row_h * (2 if big and r == 0 else 1)}">' + "".join(
            cell(t, fills[r][c] if r < len(fills) and c < len(fills[r]) else None,
                 big and r == 0, size, c) for c, t in enumerate(row[:ncol])) + "</a:tr>")
    grid = "".join(f'<a:gridCol w="{cw}"/>' for cw in col_ws)
    return (f'<p:graphicFrame><p:nvGraphicFramePr><p:cNvPr id="{sid}" name="Table"/>'
            '<p:cNvGraphicFramePr><a:graphicFrameLocks noGrp="1"/></p:cNvGraphicFramePr>'
            f'<p:nvPr/></p:nvGraphicFramePr><p:xfrm><a:off x="{x}" y="{y}"/><a:ext cx="{w}" '
            f'cy="{h}"/></p:xfrm><a:graphic><a:graphicData uri="{NS_TABLE}"><a:tbl><a:tblPr '
            f'firstRow="1"/><a:tblGrid>{grid}</a:tblGrid>{"".join(trs)}</a:tbl></a:graphicData>'
            "</a:graphic></p:graphicFrame>")


def _chart_frame(sid: int, rid: str, x: int, y: int, w: int, h: int) -> str:
    return (f'<p:graphicFrame><p:nvGraphicFramePr><p:cNvPr id="{sid}" name="Chart {sid}"/>'
            '<p:cNvGraphicFramePr/><p:nvPr/></p:nvGraphicFramePr><p:xfrm><a:off x="'
            f'{x}" y="{y}"/><a:ext cx="{w}" cy="{h}"/></p:xfrm><a:graphic><a:graphicData '
            f'uri="{NS_C}"><c:chart xmlns:c="{NS_C}" r:id="{rid}"/></a:graphicData>'
            "</a:graphic></p:graphicFrame>")


CHART_SLIDE_LINES = 3   # lines shown above a chart on its slide (more go in its footnote)


def pptx(slides: Sequence[Mapping[str, Any]]) -> bytes:
    """A deck: each slide ``{"title", "lines": [...], "chart": <drawn chart> | None}``; a
    line starting "- " is a bullet. A chart slide shows its chart (small multiples side by
    side) and a footnote of where its numbers come from."""
    slides = list(slides)[:MAX_SLIDES] or [{"title": "", "lines": []}]
    parts: dict[str, Any] = {}
    overrides = {"/ppt/presentation.xml": PML + "presentation.main+xml",
                 "/ppt/slideMasters/slideMaster1.xml": PML + "slideMaster+xml",
                 "/ppt/slideLayouts/slideLayout1.xml": PML + "slideLayout+xml",
                 "/ppt/theme/theme1.xml": "application/vnd.openxmlformats-officedocument."
                 "theme+xml"}
    chart_no = 1
    notes: dict[int, list[str]] = {}
    for n, slide in enumerate(slides, 1):
        tree = [_text_box(2, "Title", MARGIN, 300000, SLIDE_W - 2 * MARGIN, 640080,
                          [_para(str(slide.get("title") or ""), 2600, True)])]
        srels = [("rId1", "slideLayout", "../slideLayouts/slideLayout1.xml")]
        chart = slide.get("chart")
        lines = [str(t) for t in slide.get("lines") or ()]
        body_top = 1050000
        table = slide.get("table")
        slide_notes = [str(t) for t in slide.get("notes") or () if str(t).strip()]
        if table and not chart:   # a slide's own table under its few lines
            top, width = body_top, SLIDE_W - 2 * MARGIN
            if lines:
                lead = 360000 * min(len(lines), 4)
                tree.append(_text_box(3, "Body", MARGIN, top, width, lead,
                                      [_para(t[2:] if t.startswith("- ") else t, 1600,
                                             bullet=t.startswith("- ")) for t in lines[:4]]))
                top += lead + 120000
            foot_h = 720000 if slide_notes else MARGIN
            tree.append(table_frame(4, table["header"], table["rows"], [], MARGIN, top,
                                    width, SLIDE_H - top - foot_h, right=table["right"],
                                    widths=table["widths"]))
            if slide_notes:
                face = " ".join(t for t in slide_notes if not t.startswith("Statement: "))
                if face:
                    tree.append(_text_box(5, "Source", MARGIN, SLIDE_H - 680000, width,
                                          520000, [_para(face, 900, color=MUTED)]))
                notes[n] = slide_notes
        elif lines and not chart:
            tree.append(_text_box(3, "Body", MARGIN, body_top, SLIDE_W - 2 * MARGIN,
                                  SLIDE_H - body_top - MARGIN,
                                  [_para(t[2:] if t.startswith("- ") else t, 1800,
                                         bullet=t.startswith("- ")) for t in lines[:14]]))
            if slide_notes:
                notes[n] = slide_notes
        if chart:
            top, height = body_top, SLIDE_H - body_top - 900000
            width = SLIDE_W - 2 * MARGIN
            said = lines[:CHART_SLIDE_LINES]
            if said:   # a few lines of the slide's own above its chart
                lead = 330000 * len(said)
                tree.append(_text_box(3, "Body", MARGIN, top, width, lead,
                                      [_para(t[2:] if t.startswith("- ") else t, 1400,
                                             bullet=t.startswith("- ")) for t in said]))
                top, height = top + lead, height - lead
            if chart["kind"] in ox.TABLES:
                tree.append(_pptx_table(4, chart, MARGIN, top, width, height))
            else:
                made = ox.embedded_charts(chart, "ppt", chart_no)
                per = width // max(1, min(len(made), 3))
                rows_of = (len(made) + 2) // 3
                for i, (name, chart_parts) in enumerate(made):
                    parts.update(chart_parts)
                    overrides["/" + name] = ox.CT_CHART
                    rid = f"rId{i + 2}"
                    srels.append((rid, "chart", "../" + name.split("/", 1)[1]))
                    h = height // max(rows_of, 1)
                    tree.append(_chart_frame(10 + i, rid, MARGIN + per * (i % 3),
                                             top + h * (i // 3), per, h))
                chart_no += len(made)
            # never cut (the statement is the evidence): the slide says where its numbers
            # come from, the statement itself goes in the slide's speaker notes whole
            said_where = ox.provenance(chart)
            note = " ".join([t for t in said_where if not t.startswith("Statement: ")]
                            + lines[CHART_SLIDE_LINES:])
            size = 900 if len(note) <= 500 else 700 if len(note) <= 1000 else 600
            tree.append(_text_box(5, "Source", MARGIN, SLIDE_H - 820000, SLIDE_W - 2 * MARGIN,
                                  640000, [_para(note, size, color=MUTED)]))
            notes[n] = said_where
        if n in notes:
            srels.append((f"rId{len(srels) + 1}", "notesSlide",
                          f"../notesSlides/notesSlide{n}.xml"))
            parts[f"ppt/notesSlides/notesSlide{n}.xml"] = _notes_slide(notes[n])
            parts[f"ppt/notesSlides/_rels/notesSlide{n}.xml.rels"] = ox.rels(
                [("rId1", "notesMaster", "../notesMasters/notesMaster1.xml"),
                 ("rId2", "slide", f"../slides/slide{n}.xml")])
            overrides[f"/ppt/notesSlides/notesSlide{n}.xml"] = PML + "notesSlide+xml"
        parts[f"ppt/slides/slide{n}.xml"] = (
            f'{ox.XML}<p:sld {_NS}><p:cSld><p:spTree>{_TREE}{"".join(tree)}</p:spTree>'
            '</p:cSld><p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sld>')
        parts[f"ppt/slides/_rels/slide{n}.xml.rels"] = ox.rels(srels)
        overrides[f"/ppt/slides/slide{n}.xml"] = PML + "slide+xml"
    ids = "".join(f'<p:sldId id="{255 + n}" r:id="rId{n + 2}"/>'
                  for n in range(1, len(slides) + 1))
    noted = f"rId{len(slides) + 3}"
    if notes:
        parts.update({
            "ppt/notesMasters/notesMaster1.xml": (
                f'{ox.XML}<p:notesMaster {_NS}><p:cSld><p:spTree>{_TREE}'
                f'{_notes_body(2, "")}</p:spTree></p:cSld><p:clrMap bg1="lt1" tx1="dk1" '
                'bg2="lt2" tx2="dk2" accent1="accent1" accent2="accent2" accent3="accent3" '
                'accent4="accent4" accent5="accent5" accent6="accent6" hlink="hlink" '
                'folHlink="folHlink"/></p:notesMaster>'),
            "ppt/notesMasters/_rels/notesMaster1.xml.rels": ox.rels(
                [("rId1", "theme", "../theme/theme2.xml")]),
            "ppt/theme/theme2.xml": THEME})
        overrides["/ppt/notesMasters/notesMaster1.xml"] = PML + "notesMaster+xml"
        overrides["/ppt/theme/theme2.xml"] = ("application/vnd.openxmlformats-officedocument."
                                              "theme+xml")
    parts.update({
        "_rels/.rels": ox.rels([("rId1", "officeDocument", "ppt/presentation.xml")]),
        "ppt/presentation.xml": (
            f'{ox.XML}<p:presentation {_NS}><p:sldMasterIdLst><p:sldMasterId id="2147483648" '
            f'r:id="rId1"/></p:sldMasterIdLst>'
            + (f'<p:notesMasterIdLst><p:notesMasterId r:id="{noted}"/></p:notesMasterIdLst>'
               if notes else "")
            + f'<p:sldIdLst>{ids}</p:sldIdLst><p:sldSz '
            f'cx="{SLIDE_W}" cy="{SLIDE_H}"/><p:notesSz cx="6858000" cy="9144000"/>'
            "</p:presentation>"),
        "ppt/_rels/presentation.xml.rels": ox.rels(
            [("rId1", "slideMaster", "slideMasters/slideMaster1.xml"),
             ("rId2", "theme", "theme/theme1.xml")]
            + [(f"rId{n + 2}", "slide", f"slides/slide{n}.xml")
               for n in range(1, len(slides) + 1)]
            + ([(noted, "notesMaster", "notesMasters/notesMaster1.xml")] if notes else [])),
        "ppt/slideMasters/slideMaster1.xml": (
            f'{ox.XML}<p:sldMaster {_NS}><p:cSld><p:spTree>{_TREE}</p:spTree></p:cSld>'
            '<p:clrMap bg1="lt1" tx1="dk1" bg2="lt2" tx2="dk2" accent1="accent1" '
            'accent2="accent2" accent3="accent3" accent4="accent4" accent5="accent5" '
            'accent6="accent6" hlink="hlink" folHlink="folHlink"/><p:sldLayoutIdLst>'
            '<p:sldLayoutId id="2147483649" r:id="rId1"/></p:sldLayoutIdLst></p:sldMaster>'),
        "ppt/slideMasters/_rels/slideMaster1.xml.rels": ox.rels(
            [("rId1", "slideLayout", "../slideLayouts/slideLayout1.xml"),
             ("rId2", "theme", "../theme/theme1.xml")]),
        "ppt/slideLayouts/slideLayout1.xml": (
            f'{ox.XML}<p:sldLayout {_NS} type="blank" preserve="1"><p:cSld name="Blank">'
            f"<p:spTree>{_TREE}</p:spTree></p:cSld><p:clrMapOvr><a:masterClrMapping/>"
            "</p:clrMapOvr></p:sldLayout>"),
        "ppt/slideLayouts/_rels/slideLayout1.xml.rels": ox.rels(
            [("rId1", "slideMaster", "../slideMasters/slideMaster1.xml")]),
        "ppt/theme/theme1.xml": THEME})
    types = ox.content_types({**ox.BASE_TYPES, "xlsx": ox.CT_XLSX}, overrides)
    return ox.zip_parts({"[Content_Types].xml": types, **parts})


def _notes_body(sid: int, text: str) -> str:
    """The notes page's text placeholder holding ``text`` (paragraph per line)."""
    paras = "".join(f'<a:p><a:r><a:rPr lang="en-US" dirty="0"/><a:t>{escape(t)}</a:t></a:r></a:p>'
                    for t in text.split("\n")) if text else "<a:p/>"
    return (f'<p:sp><p:nvSpPr><p:cNvPr id="{sid}" name="Notes"/><p:cNvSpPr><a:spLocks '
            'noGrp="1"/></p:cNvSpPr><p:nvPr><p:ph type="body" idx="1"/></p:nvPr></p:nvSpPr>'
            '<p:spPr><a:xfrm><a:off x="685800" y="4343400"/><a:ext cx="5486400" cy="4114800"/>'
            '</a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom></p:spPr><p:txBody>'
            f"<a:bodyPr/><a:lstStyle/>{paras}</p:txBody></p:sp>")


def _notes_slide(lines: Sequence[str]) -> str:
    return (f'{ox.XML}<p:notes {_NS}><p:cSld><p:spTree>{_TREE}'
            f'{_notes_body(2, chr(10).join(lines))}</p:spTree></p:cSld><p:clrMapOvr>'
            "<a:masterClrMapping/></p:clrMapOvr></p:notes>")


# ----------------------------------------------------------------------------- Word

NS_WP = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
DOC_NS = (f'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
          f'xmlns:r="{NS_R}" xmlns:wp="{NS_WP}" xmlns:a="{NS_A}" xmlns:c="{NS_C}"')


def _w_run(text: str, *, bold: bool = False, size: int = 0, color: str = "") -> str:
    props = ("<w:b/>" if bold else "") + (f'<w:color w:val="{color}"/>' if color else "") \
        + (f'<w:sz w:val="{size}"/>' if size else "")
    return (f"<w:r>{f'<w:rPr>{props}</w:rPr>' if props else ''}"
            f'<w:t xml:space="preserve">{escape(text)}</w:t></w:r>')


def _w_table(chart: Mapping[str, Any]) -> str:
    header, rows, fills = _grid(chart)
    big = chart["kind"] == "kpi"
    edge = "".join(f'<w:{side} w:val="single" w:sz="4" w:space="0" w:color="D1D5DB"/>'
                   for side in ("top", "left", "bottom", "right", "insideH", "insideV"))

    # a wide table keeps each cell on one line within the page: smaller type, tight margins
    ncol = max(len(header), 1)
    small = 0 if ncol <= 6 else 16 if ncol <= 9 else 14

    def tc(text: str, fill: Optional[str], bold: bool = False, size: int = 0) -> str:
        shd = f'<w:tcPr><w:shd w:val="clear" w:color="auto" w:fill="{fill}"/></w:tcPr>' \
            if fill else ""
        run = _w_run(text, bold=bold, size=size or small, color=ox.ink(fill) if fill else "")
        return f"<w:tc>{shd}<w:p>{run}</w:p></w:tc>"
    body = "<w:tr>" + "".join(tc(h.replace("_", " "), "F3F4F6", True) for h in header) \
        + "</w:tr>"
    for r, row in enumerate(rows[:200]):
        body += "<w:tr>" + "".join(
            tc(t, fills[r][c] if r < len(fills) and c < len(fills[r]) else None,
               big and r == 0, 40 if big and r == 0 else 0) for c, t in enumerate(row)) \
            + "</w:tr>"
    margins = ('<w:tblCellMar><w:left w:w="40" w:type="dxa"/><w:right w:w="40" '
               'w:type="dxa"/></w:tblCellMar>') if small else ""
    return (f'<w:tbl><w:tblPr><w:tblW w:w="0" w:type="auto"/><w:tblBorders>{edge}'
            f"</w:tblBorders>{margins}</w:tblPr>{body}</w:tbl>")


def docx_chart(chart: Mapping[str, Any], first: int) -> tuple[str, dict[str, Any],
                                                               list[tuple[str, str, str]],
                                                               dict[str, str]]:
    """Word body XML for ``chart`` (its title, the chart or a coloured table, its source
    note) with the parts, document relationships and content types it needs; charts are
    numbered from ``first`` (relationship ids ``rIdC<n>``)."""
    title = f"<w:p>{_w_run(str(chart.get('title') or ''), bold=True, size=24)}</w:p>"
    note = "".join(f"<w:p>{_w_run(line, size=16, color=MUTED)}</w:p>"
                   for line in ox.provenance(chart))
    if chart["kind"] in ox.TABLES:
        return title + _w_table(chart) + note, {}, [], {}
    parts: dict[str, Any] = {}
    relations, overrides, body = [], {}, []
    made = ox.embedded_charts(chart, "word", first)
    for i, (name, chart_parts) in enumerate(made):
        n = first + i
        parts.update(chart_parts)
        overrides["/" + name] = ox.CT_CHART
        relations.append((f"rIdC{n}", "chart", name.split("/", 1)[1]))
        cx, cy = (5486400, 3200400) if len(made) == 1 else (2700000, 1900000)
        body.append(
            f'<w:r><w:drawing><wp:inline distT="0" distB="0" distL="0" distR="0"><wp:extent '
            f'cx="{cx}" cy="{cy}"/><wp:effectExtent l="0" t="0" r="0" b="0"/><wp:docPr '
            f'id="{1000 + n}" name="Chart {n}"/><wp:cNvGraphicFramePr/><a:graphic>'
            f'<a:graphicData uri="{NS_C}"><c:chart r:id="rIdC{n}"/></a:graphicData>'
            "</a:graphic></wp:inline></w:drawing></w:r>")
    return title + "<w:p>" + "".join(body) + "</w:p>" + note, parts, relations, overrides
