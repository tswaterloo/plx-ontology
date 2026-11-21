"""Charts as Office files, standard library only: a drawn chart (``operator_api.charts``,
the ``models.AskChart`` shape) written as an Excel workbook (its rows on a sheet and a native
chart over those cells, so it stays live when a number is edited), a PowerPoint slide or a
Word page (a native chart whose data is the embedded workbook: "Edit data" opens it).

Kinds Office draws natively (bar, stacked, horizontal, line, area, combo on two axes, pie,
doughnut, scatter with its fitted line, histogram, funnel, waterfall, small multiples) are
charts; a heatmap, a coloured table and KPI tiles are tables coloured by value. Every value
written is one the chart holds (cells its rows returned, or the arithmetic the chart states:
a pie's remainder, histogram counts, a waterfall's running total); a file also states the
rows it was drawn from, the as-of time and the statement. Evidence: isolated-fixture.
"""

from __future__ import annotations

import io
import math
import zipfile
from typing import Any, Mapping, Optional, Sequence
from xml.sax.saxutils import escape

# one categorical palette (as the app's light theme), the sequential ramp ends, waterfall
PALETTE = ("2E6FB7", "E3832F", "2A9D74", "C9455B", "7C5CBF", "9C7A2E", "3FA7C9", "7F7F7F")
RAMP = ((0xEE, 0xF4, 0xFB), (0x1F, 0x5A, 0x99))
UP, DOWN, TOTAL = "2E6FB7", "E3832F", "4B5563"
GRID, AXIS_TEXT = "E5E7EB", "5B6170"
NATIVE = frozenset({"bar", "line", "area", "combo", "pie", "donut", "scatter", "histogram",
                    "funnel", "waterfall"})
TABLES = frozenset({"heatmap", "table", "kpi"})
EMU = 12700   # per point
_FIXED = (2026, 1, 1, 0, 0, 0)
NS_C = "http://schemas.openxmlformats.org/drawingml/2006/chart"
NS_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
NS_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS_P = "http://schemas.openxmlformats.org/presentationml/2006/main"
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
XML = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
CT_CHART = "application/vnd.openxmlformats-officedocument.drawingml.chart+xml"
CT_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def zip_parts(parts: Mapping[str, Any]) -> bytes:
    """A package of ``parts`` (text or bytes) with fixed timestamps: the same chart makes
    the same bytes."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in parts.items():
            info = zipfile.ZipInfo(name, _FIXED)
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, data if isinstance(data, bytes) else data.encode("utf-8"))
    return out.getvalue()


def rels(items: Sequence[tuple[str, str, str]]) -> str:
    """A relationships part: (id, type suffix or full type, target)."""
    body = "".join(f'<Relationship Id="{i}" Type="{t if "://" in t else REL + "/" + t}" '
                   f'Target="{escape(target)}"/>' for i, t, target in items)
    return f'{XML}<Relationships xmlns="{PKG_REL}">{body}</Relationships>'


def content_types(defaults: Mapping[str, str], overrides: Mapping[str, str]) -> str:
    d = "".join(f'<Default Extension="{e}" ContentType="{t}"/>' for e, t in defaults.items())
    o = "".join(f'<Override PartName="{p}" ContentType="{t}"/>' for p, t in overrides.items())
    return (f'{XML}<Types xmlns="http://schemas.openxmlformats.org/package/2006/'
            f'content-types">{d}{o}</Types>')


BASE_TYPES = {"rels": "application/vnd.openxmlformats-package.relationships+xml",
              "xml": "application/xml"}


# ----------------------------------------------------------------------------- the data

def column(n: int) -> str:
    out = ""
    n += 1
    while n:
        n, r = divmod(n - 1, 26)
        out = chr(65 + r) + out
    return out


def _num(v: Any) -> Optional[float]:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def nice(v: float) -> float:
    """``v`` rounded away from zero to 1, 2, 2.5 or 5 times a power of ten (an axis end)."""
    if not v:
        return 0.0
    step = 10 ** math.floor(math.log10(abs(v)))
    for m in (1, 2, 2.5, 5, 10):
        if abs(v) <= m * step + 1e-9 * step:
            return math.copysign(m * step, v)
    return v


def blocks(chart: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The chart as sheet blocks, each a native chart's data (or a table): ``header``,
    ``rows`` (label then values), and the chart-only columns (``helper``: a waterfall's
    running start and its rises and falls)."""
    kind = chart["kind"]
    panels = chart.get("panels")
    if panels:
        # small multiples share one value scale, so panels compare by eye
        stacked = bool((chart.get("spec") or {}).get("stacked"))
        sums: list[float] = [0.0]
        for p in panels:
            for i in range(len(p["labels"])):
                vals = [v for s in p["series"] for v in [_num(s["values"][i])] if v is not None]
                sums += ([sum(v for v in vals if v > 0), sum(v for v in vals if v < 0)]
                         if stacked else vals)
        scale = (nice(min(sums)), nice(max(sums)))
        return [{"title": p["title"], "header": [chart.get("x") or "", *[s["name"] for s in
                                                                         p["series"]]],
                 "rows": [[label, *[_num(s["values"][i]) for s in p["series"]]]
                          for i, label in enumerate(p["labels"])], "series": p["series"],
                 "scale": scale}
                for p in panels]
    series = list(chart.get("series") or ())
    labels = list(chart.get("labels") or ())
    if kind == "scatter":
        name = (chart.get("spec") or {}).get("label") or chart.get("x") or "point"
        xs = chart.get("xs") or []
        return [{"title": chart.get("title", ""),
                 "header": [name, chart.get("x") or "x", *[s["name"] for s in series]],
                 "rows": [[label, _num(xs[i]) if i < len(xs) else None,
                           *[_num(s["values"][i]) for s in series]]
                          for i, label in enumerate(labels)], "series": series}]
    head = chart.get("x") or ("bin" if kind == "histogram" else "")
    block = {"title": chart.get("title", ""), "header": [head, *[s["name"] for s in series]],
             "rows": [[label, *[_num(s["values"][i]) if i < len(s["values"]) else None
                                for s in series]] for i, label in enumerate(labels)],
             "series": series}
    if kind == "waterfall":
        run, helper = 0.0, []
        for row in block["rows"]:
            change = row[1] or 0.0
            start, end = run, run + change
            helper.append([min(start, end), change if change >= 0 else None,
                           -change if change < 0 else None, None])
            run = end
        helper.append([0.0, None, None, run])
        block["rows"].append(["Total", None])
        block["helper"] = (["Bar starts at", "Rise", "Fall", "Total"], helper)
        block["total"] = run
    return [block]


def provenance(chart: Mapping[str, Any]) -> list[str]:
    """What a file says about where its numbers come from."""
    count = chart.get('rows') or len(chart.get('labels') or ())
    lines = [f"Source: {count} {'record' if count == 1 else 'records'}"
             + (f", as of {str(chart['as_of'])[:10]}" if chart.get("as_of") else "") + "."]
    if chart.get("partial"):
        lines.append("The query returned only its first rows: this is of those rows.")
    if chart.get("shown") and chart.get("rows") and chart["shown"] < chart["rows"]:
        lines.append(f"The chart shows {chart['shown']} of them.")
    if chart.get("omitted"):
        lines.append(f"{chart['omitted']} more series are not drawn.")
    if chart.get("currency"):
        lines.append(f"Amounts in {chart['currency']}.")
    if chart.get("open_period"):
        lines.append(f"{chart['open_period']} is not over yet: its figures are of part of it.")
    if chart.get("first_period"):
        lines.append(f"{chart['first_period']} is only partly inside the window: its figures are "
                     "of part of it.")
    if chart.get("skipped"):
        lines.append(f"{chart['skipped']} " + ("row has no value and is not counted."
                     if chart["skipped"] == 1 else "rows have no value and are not counted."))
    if chart.get("kind") == "waterfall":
        lines.append("Each bar starts where the one before ended; the last bar is the total.")
    if chart.get("kind") == "funnel":
        lines.append("Each percentage is of the step before.")
    trend = chart.get("trend")
    if trend:
        lines.append(f"Fitted line y = {trend['slope']:.4g} x + {trend['intercept']:.4g}; "
                     f"r = {trend['r']:.2f} over {trend['n']} points (how closely they move "
                     "together, not that one causes the other).")
    if chart.get("sql"):
        lines.append("Statement: " + " ".join(str(chart["sql"]).split()))
    return lines


# ----------------------------------------------------------------------------- chart XML

def _fill(rgb: Optional[str]) -> str:
    return f'<a:solidFill><a:srgbClr val="{rgb}"/></a:solidFill>' if rgb else "<a:noFill/>"


def _sp(fill: Optional[str] = None, line: Optional[str] = None, width: int = 0,
        dash: str = "") -> str:
    ln = (f'<a:ln w="{width or 22225}" cap="rnd">{_fill(line)}'
          + (f'<a:prstDash val="{dash}"/>' if dash else "") + "</a:ln>") if line else (
        "<a:ln><a:noFill/></a:ln>" if fill is not None else "")
    return f"<c:spPr>{_fill(fill) if fill is not None else ''}{ln}</c:spPr>"


def _str_ref(ref: str, values: Sequence[str]) -> str:
    pts = "".join(f'<c:pt idx="{i}"><c:v>{escape(str(v))}</c:v></c:pt>'
                  for i, v in enumerate(values))
    return (f"<c:strRef><c:f>{ref}</c:f><c:strCache><c:ptCount val=\"{len(values)}\"/>"
            f"{pts}</c:strCache></c:strRef>")


def _num_ref(ref: str, values: Sequence[Optional[float]]) -> str:
    pts = "".join(f'<c:pt idx="{i}"><c:v>{v!r}</c:v></c:pt>'
                  for i, v in enumerate(values) if v is not None)
    return (f"<c:numRef><c:f>{ref}</c:f><c:numCache><c:formatCode>General</c:formatCode>"
            f"<c:ptCount val=\"{len(values)}\"/>{pts}</c:numCache></c:numRef>")


def _text_props(size: int = 900, bold: bool = False) -> str:
    return (f'<c:txPr><a:bodyPr/><a:lstStyle/><a:p><a:pPr><a:defRPr sz="{size}"'
            f'{" b=\"1\"" if bold else ""}><a:solidFill><a:srgbClr val="{AXIS_TEXT}"/>'
            "</a:solidFill></a:defRPr></a:pPr><a:endParaRPr lang=\"en-US\"/></a:p></c:txPr>")


def _ax_title(text: Optional[str], pos: str) -> str:
    """An axis's name (what its numbers or labels are), turned up the side of a left or
    right axis."""
    if not text:
        return ""
    rot = ' rot="-5400000" vert="horz"' if pos in ("l", "r") else ""
    return (f'<c:title><c:tx><c:rich><a:bodyPr{rot}/><a:lstStyle/><a:p><a:pPr><a:defRPr '
            f'sz="900" b="0"/></a:pPr><a:r><a:rPr lang="en-US" sz="900" b="0"/><a:t>'
            f'{escape(text.replace("_", " "))}</a:t></a:r></a:p></c:rich></c:tx>'
            '<c:overlay val="0"/></c:title>')


def _cat_ax(ax: int, cross: int, pos: str = "b", delete: bool = False,
            reverse: bool = False, title: Optional[str] = None) -> str:
    return (f'<c:catAx><c:axId val="{ax}"/><c:scaling><c:orientation val="'
            f'{"maxMin" if reverse else "minMax"}"/></c:scaling><c:delete val="'
            f'{int(delete)}"/><c:axPos val="{pos}"/>{_ax_title(title, pos)}'
            '<c:numFmt formatCode="General" '
            'sourceLinked="0"/><c:majorTickMark val="none"/><c:minorTickMark val="none"/>'
            f'<c:tickLblPos val="nextTo"/>{_sp(line=GRID, width=9525)}{_text_props()}'
            f'<c:crossAx val="{cross}"/><c:crosses val="autoZero"/><c:auto val="1"/>'
            '<c:lblAlgn val="ctr"/><c:lblOffset val="100"/><c:noMultiLvlLbl val="0"/>'
            "</c:catAx>")


def _val_ax(ax: int, cross: int, pos: str = "l", grid: bool = True,
            crosses: str = "autoZero", between: str = "between", fmt: str = "#,##0.##",
            scale: Optional[Sequence[float]] = None, title: Optional[str] = None) -> str:
    lines = (f"<c:majorGridlines>{_sp(line=GRID, width=6350)}</c:majorGridlines>"
             if grid else "")
    ends = (f'<c:max val="{scale[1]!r}"/><c:min val="{scale[0]!r}"/>'
            if scale and scale[1] > scale[0] else "")
    return (f'<c:valAx><c:axId val="{ax}"/><c:scaling><c:orientation val="minMax"/>{ends}'
            f'</c:scaling><c:delete val="0"/><c:axPos val="{pos}"/>{lines}{_ax_title(title, pos)}'
            f'<c:numFmt formatCode="{escape(fmt)}" sourceLinked="0"/><c:majorTickMark '
            'val="none"/><c:minorTickMark val="none"/><c:tickLblPos val="nextTo"/>'
            f'<c:spPr><a:ln><a:noFill/></a:ln></c:spPr>{_text_props()}<c:crossAx '
            f'val="{cross}"/><c:crosses val="{crosses}"/><c:crossBetween val="{between}"/>'
            "</c:valAx>")


def chart_xml(chart: Mapping[str, Any], block: Mapping[str, Any], *, sheet: str = "Data",
              top: int = 1, title: Optional[str] = None, embedded: bool = False) -> str:
    """The native chart of one block whose header row is sheet row ``top`` (1-based)."""
    kind = chart["kind"]
    spec = chart.get("spec") or {}
    n = len(block["rows"])
    first, last = top + 1, top + n
    q = f"'{sheet}'"
    cat_ref = f"{q}!$A${first}:$A${last}"
    labels = [str(r[0]) for r in block["rows"]]

    def col_ref(c: int) -> str:
        return f"{q}!${column(c)}${first}:${column(c)}${last}"

    def name_ref(c: int) -> str:
        return _str_ref(f"{q}!${column(c)}${top}", [block["header"][c]
                                                    if c < len(block["header"]) else ""])

    def values(c: int) -> list[Optional[float]]:
        return [r[c] if c < len(r) else None for r in block["rows"]]

    def ser(i: int, c: int, color: str, *, kind_: str, xcol: Optional[int] = None,
            extra: str = "") -> str:
        head = f'<c:idx val="{i}"/><c:order val="{i}"/><c:tx>{name_ref(c)}</c:tx>'
        if kind_ == "line":
            body = (_sp(line=color, width=28575) + '<c:marker><c:symbol val="circle"/>'
                    f'<c:size val="4"/>{_sp(fill=color, line=color, width=9525)}</c:marker>'
                    f"{extra}<c:cat>{_str_ref(cat_ref, labels)}</c:cat><c:val>"
                    f"{_num_ref(col_ref(c), values(c))}</c:val><c:smooth val=\"0\"/>")
        elif kind_ == "scatter":
            body = ('<c:spPr><a:ln w="19050"><a:noFill/></a:ln></c:spPr>'
                    f'<c:marker><c:symbol val="circle"/><c:size val="6"/>'
                    f"{_sp(fill=color, line=color, width=9525)}</c:marker>{extra}"
                    f"<c:xVal>{_num_ref(col_ref(xcol or 1), values(xcol or 1))}</c:xVal>"
                    f"<c:yVal>{_num_ref(col_ref(c), values(c))}</c:yVal>"
                    '<c:smooth val="0"/>')
        elif kind_ == "area":
            body = (f"{_sp(fill=color, line=color, width=12700)}{extra}<c:cat>"
                    f"{_str_ref(cat_ref, labels)}</c:cat><c:val>"
                    f"{_num_ref(col_ref(c), values(c))}</c:val>")
        elif kind_ == "pie":
            pts = "".join(f'<c:dPt><c:idx val="{k}"/><c:bubble3D val="0"/>'
                          f"{_sp(fill=PALETTE[k % len(PALETTE)], line='FFFFFF', width=12700)}"
                          "</c:dPt>" for k in range(n))
            body = (f"{pts}<c:dLbls><c:numFmt formatCode=\"0%\" sourceLinked=\"0\"/>"
                    "<c:spPr><a:noFill/><a:ln><a:noFill/></a:ln></c:spPr>"
                    f"{_text_props(900)}<c:showLegendKey val=\"0\"/><c:showVal val=\"0\"/>"
                    '<c:showCatName val="0"/><c:showSerName val="0"/><c:showPercent '
                    'val="1"/><c:showBubbleSize val="0"/><c:showLeaderLines val="1"/>'
                    f"</c:dLbls><c:cat>{_str_ref(cat_ref, labels)}</c:cat><c:val>"
                    f"{_num_ref(col_ref(c), values(c))}</c:val>")
        else:   # bar
            fill = _sp(fill=color)
            body = (f'{fill}<c:invertIfNegative val="0"/>{extra}<c:cat>'
                    f"{_str_ref(cat_ref, labels)}</c:cat><c:val>"
                    f"{_num_ref(col_ref(c), values(c))}</c:val>")
        return f"<c:ser>{head}{body}</c:ser>"

    count = len(block["header"]) - 1
    plot = ""
    legend = count > 1 or kind in ("pie", "donut")
    # the axes' names: the label column, and the value column when no legend names it
    xname = str(chart.get("x") or "") or None
    names = [str(h) for h in block["header"][1:count + 1]]
    vname = "rows" if kind == "histogram" else names[0] if count == 1 else None
    if kind in ("bar", "histogram", "funnel"):
        horizontal = bool(spec.get("horizontal")) or kind == "funnel"
        stacked = bool(spec.get("stacked"))
        series = "".join(ser(i, i + 1, PALETTE[i % len(PALETTE)], kind_="bar")
                         for i in range(count))
        plot = (f'<c:barChart><c:barDir val="{"bar" if horizontal else "col"}"/><c:grouping '
                f'val="{"stacked" if stacked else "clustered"}"/><c:varyColors val="0"/>'
                f'{series}<c:gapWidth val="{6 if kind == "histogram" else 60}"/>'
                + ('<c:overlap val="100"/>' if stacked else "")
                + '<c:axId val="1"/><c:axId val="2"/></c:barChart>'
                + _cat_ax(1, 2, "l" if horizontal else "b", reverse=horizontal, title=xname)
                + _val_ax(2, 1, "b" if horizontal else "l", scale=block.get("scale"),
                          title=vname))
    elif kind == "waterfall":
        names, helper = block["helper"]
        # the helper columns sit right of the value column: start, rise, fall
        hc = len(block["header"])

        def hser(i: int, c: int, color: Optional[str], vals: list) -> str:
            ref = f"{q}!${column(c)}${first}:${column(c)}${last}"
            fill = _sp(fill=color) if color else ("<c:spPr><a:noFill/><a:ln><a:noFill/>"
                                                  "</a:ln></c:spPr>")
            return (f'<c:ser><c:idx val="{i}"/><c:order val="{i}"/><c:tx>'
                    f"{_str_ref(f'{q}!${column(c)}${top}', [names[i]])}"
                    f'</c:tx>{fill}<c:invertIfNegative val="0"/><c:cat>'
                    f"{_str_ref(cat_ref, labels)}</c:cat><c:val>{_num_ref(ref, vals)}"
                    "</c:val></c:ser>")
        series = (hser(0, hc, None, [h[0] for h in helper])
                  + hser(1, hc + 1, UP, [h[1] for h in helper])
                  + hser(2, hc + 2, DOWN, [h[2] for h in helper])
                  + hser(3, hc + 3, TOTAL, [h[3] for h in helper]))
        plot = ('<c:barChart><c:barDir val="col"/><c:grouping val="stacked"/><c:varyColors '
                f'val="0"/>{series}<c:gapWidth val="50"/><c:overlap val="100"/>'
                '<c:axId val="1"/><c:axId val="2"/></c:barChart>' + _cat_ax(1, 2, title=xname)
                + _val_ax(2, 1, title=names[0] if names else None))
        legend = False
    elif kind in ("line", "area"):
        stacked = bool(spec.get("stacked"))
        tag = "lineChart" if kind == "line" else "areaChart"
        series = "".join(ser(i, i + 1, PALETTE[i % len(PALETTE)], kind_=kind)
                         for i in range(count))
        plot = (f'<c:{tag}><c:grouping val="{"stacked" if stacked else "standard"}"/>'
                f'<c:varyColors val="0"/>{series}'
                + ('<c:marker val="1"/>' if kind == "line" else "")
                + f'<c:axId val="1"/><c:axId val="2"/></c:{tag}>'
                + _cat_ax(1, 2, title=xname)
                + _val_ax(2, 1, scale=block.get("scale"), title=vname))
    elif kind == "combo":
        right = [i for i, s in enumerate(block["series"]) if s.get("axis") == "right"]
        left = [i for i in range(count) if i not in right]
        bars = "".join(ser(i, i + 1, PALETTE[i % len(PALETTE)], kind_="bar") for i in left)
        lines = "".join(ser(i, i + 1, PALETTE[i % len(PALETTE)], kind_="line") for i in right)
        plot = ('<c:barChart><c:barDir val="col"/><c:grouping val="clustered"/><c:varyColors '
                f'val="0"/>{bars}<c:gapWidth val="60"/><c:axId val="1"/><c:axId val="2"/>'
                '</c:barChart><c:lineChart><c:grouping val="standard"/><c:varyColors val="0"/>'
                f'{lines}<c:marker val="1"/><c:axId val="3"/><c:axId val="4"/></c:lineChart>'
                + _cat_ax(1, 2, title=xname)
                + _val_ax(2, 1, title=", ".join(names[i] for i in left))
                + _cat_ax(3, 4, delete=True)
                + _val_ax(4, 3, "r", grid=False, crosses="max",
                          title=", ".join(names[i] for i in right)))
    elif kind in ("pie", "donut"):
        tag = "pieChart" if kind == "pie" else "doughnutChart"
        plot = (f'<c:{tag}><c:varyColors val="1"/>{ser(0, 1, PALETTE[0], kind_="pie")}'
                '<c:firstSliceAng val="0"/>' + ('<c:holeSize val="55"/>' if kind == "donut"
                                                 else "") + f"</c:{tag}>")
    elif kind == "scatter":
        trend = ""
        if chart.get("trend"):
            trend = (f"<c:trendline>{_sp(line='4B5563', width=19050, dash='dash')}"
                     '<c:trendlineType val="linear"/><c:dispRSqr val="0"/><c:dispEq val="0"/>'
                     "</c:trendline>")
        series = "".join(ser(i, i + 2, PALETTE[i % len(PALETTE)], kind_="scatter", xcol=1,
                             extra=trend if i == 0 else "")
                         for i in range(count - 1))
        plot = ('<c:scatterChart><c:scatterStyle val="lineMarker"/><c:varyColors val="0"/>'
                f'{series}<c:axId val="1"/><c:axId val="2"/></c:scatterChart>'
                + _val_ax(1, 2, "b", grid=False, between="midCat",
                          title=names[0] if names else None)
                + _val_ax(2, 1, "l", between="midCat",
                          title=names[1] if count == 2 else None))
        legend = count > 2
    heading = escape(title if title is not None else str(chart.get("title") or ""))
    title_xml = (f'<c:title><c:tx><c:rich><a:bodyPr/><a:lstStyle/><a:p><a:pPr><a:defRPr '
                 f'sz="1200" b="1"/></a:pPr><a:r><a:rPr lang="en-US" sz="1200" b="1"/>'
                 f"<a:t>{heading}</a:t></a:r></a:p></c:rich></c:tx><c:overlay val=\"0\"/>"
                 '</c:title><c:autoTitleDeleted val="0"/>' if heading
                 else '<c:autoTitleDeleted val="1"/>')
    legend_xml = ('<c:legend><c:legendPos val="b"/><c:overlay val="0"/>'
                  f"{_text_props()}</c:legend>" if legend else "")
    external = ('<c:externalData r:id="rId1"><c:autoUpdate val="0"/></c:externalData>'
                if embedded else "")
    return (f'{XML}<c:chartSpace xmlns:c="{NS_C}" xmlns:a="{NS_A}" xmlns:r="{NS_R}">'
            '<c:roundedCorners val="0"/>'
            f"<c:chart>{title_xml}<c:plotArea><c:layout/>{plot}</c:plotArea>{legend_xml}"
            '<c:plotVisOnly val="1"/><c:dispBlanksAs val="gap"/></c:chart>'
            '<c:spPr><a:noFill/><a:ln><a:noFill/></a:ln></c:spPr>'
            f"{_text_props(1000)}{external}</c:chartSpace>")


# ----------------------------------------------------------------------------- tables

def ramp(value: Optional[float], lo: float, hi: float) -> Optional[str]:
    """The sequential colour of ``value`` between ``lo`` and ``hi`` (hex), None for none."""
    if value is None:
        return None
    t = 0.0 if hi <= lo else max(0.0, min(1.0, (value - lo) / (hi - lo)))
    return "".join(f"{round(a + (b - a) * t):02X}" for a, b in zip(*RAMP))


def ink(fill: Optional[str]) -> str:
    """Dark or white text, whichever reads on ``fill``."""
    if not fill:
        return "1F2937"
    r, g, b = (int(fill[i:i + 2], 16) for i in (0, 2, 4))
    return "1F2937" if 0.2126 * r + 0.7152 * g + 0.0722 * b > 140 else "FFFFFF"


def colour_grid(chart: Mapping[str, Any]) -> tuple[list[str], list[list[Any]],
                                                     list[list[Optional[str]]]]:
    """(header, rows, fills) of a heatmap (one scale over every cell, its rows the series)
    or a coloured table / KPI (one scale per column)."""
    labels, series = list(chart.get("labels") or ()), list(chart.get("series") or ())
    if chart["kind"] == "heatmap":
        header = [(chart.get("spec") or {}).get("split") or "", *labels]
        rows = [[s["name"], *s["values"]] for s in series]
        cells = [v for s in series for v in s["values"] if v is not None]
        lo, hi = (min(cells), max(cells)) if cells else (0.0, 0.0)
        fills = [[None, *[ramp(v, lo, hi) for v in s["values"]]] for s in series]
        return header, rows, fills
    header = [chart.get("x") or "", *[s["name"] for s in series]]
    rows = [[label, *[s["values"][i] for s in series]] for i, label in enumerate(labels)]
    fills: list[list[Optional[str]]] = [[None] for _ in rows]
    for s in series:
        vals = [v for v in s["values"] if v is not None]
        lo, hi = (min(vals), max(vals)) if vals else (0.0, 0.0)
        for i, v in enumerate(s["values"]):
            fills[i].append(ramp(v, lo, hi) if chart["kind"] == "table" else None)
    return header, rows, fills


def fmt(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:,.0f}" if value == int(value) else f"{value:,.2f}"
    return str(value)


# ----------------------------------------------------------------------------- xlsx

def _cell(ref: str, value: Any, style: int = 0) -> str:
    s = f' s="{style}"' if style else ""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f'<c r="{ref}"{s}><v>{value!r}</v></c>'
    if value is None or value == "":
        return ""
    return (f'<c r="{ref}"{s} t="inlineStr"><is><t xml:space="preserve">{escape(str(value))}'
            "</t></is></c>")


STYLES = (f'{XML}<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
          '<numFmts count="1"><numFmt numFmtId="164" formatCode="#,##0.00"/></numFmts>'
          '<fonts count="3"><font><sz val="11"/><name val="Calibri"/></font><font><b/><sz '
          'val="11"/><name val="Calibri"/></font><font><i/><sz val="9"/><color rgb="FF6B7280"/>'
          '<name val="Calibri"/></font></fonts><fills count="2"><fill><patternFill '
          'patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills>'
          '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border>'
          '</borders><cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" '
          'borderId="0"/></cellStyleXfs><cellXfs count="5"><xf numFmtId="0" fontId="0" '
          'fillId="0" borderId="0" xfId="0"/><xf numFmtId="0" fontId="1" fillId="0" '
          'borderId="0" xfId="0" applyFont="1"/><xf numFmtId="0" fontId="2" fillId="0" '
          'borderId="0" xfId="0" applyFont="1" applyAlignment="1"><alignment wrapText="1" '
          'vertical="top"/></xf><xf numFmtId="164" fontId="0" fillId="0" '
          'borderId="0" xfId="0" applyNumberFormat="1"/><xf numFmtId="3" fontId="0" '
          'fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/></cellXfs></styleSheet>')


def _num_style(value: Any) -> int:
    """A number cell's style: thousands separated, two decimals unless it is whole."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return 4 if float(value) == int(value) else 3


def _sheet_rows(chart: Mapping[str, Any]) -> tuple[list[str], list[tuple[int, dict]], list[int],
                                                     list[str]]:
    """Sheet row XML (where the numbers come from first, then the rows), the blocks with
    their header row (1-based), each column's width in characters and the colour-scale
    ranges."""
    out: list[str] = []
    placed: list[tuple[int, dict]] = []
    scales: list[str] = []
    widths: list[int] = []

    def fit(cells: Sequence[Any]) -> None:
        for c, v in enumerate(cells):
            text = fmt(v) if isinstance(v, float) else str(v if v is not None else "")
            if c >= len(widths):
                widths.append(8)
            widths[c] = min(40 if c == 0 else 24, max(widths[c], len(text) + 2))

    r = len(provenance(chart)) + 2   # where the numbers come from is above them
    if chart["kind"] in TABLES:
        header, rows, _ = colour_grid(chart)
        out.append(f'<row r="{r}">' + "".join(_cell(f"{column(c)}{r}", h, 1)
                                              for c, h in enumerate(header)) + "</row>")
        fit(header)
        for i, row in enumerate(rows):
            rr = r + 1 + i
            out.append(f'<row r="{rr}">' + "".join(
                _cell(f"{column(c)}{rr}", v, _num_style(v)) for c, v in enumerate(row))
                + "</row>")
            fit(row)
        if rows and chart["kind"] == "heatmap":
            scales.append(f"B{r + 1}:{column(len(header) - 1)}{r + len(rows)}")
        elif rows and chart["kind"] == "table":
            scales += [f"{column(c)}{r + 1}:{column(c)}{r + len(rows)}"
                       for c in range(1, len(header))]
    else:
        for block in blocks(chart):
            if len(blocks(chart)) > 1:
                out.append(f'<row r="{r}">{_cell(f"A{r}", block["title"], 1)}</row>')
                r += 1
            header = list(block["header"])
            helper = block.get("helper")
            if helper:
                header += helper[0]
            out.append(f'<row r="{r}">' + "".join(_cell(f"{column(c)}{r}", h, 1)
                                                  for c, h in enumerate(header)) + "</row>")
            fit(header)
            placed.append((r, block))
            for i, row in enumerate(block["rows"]):
                rr = r + 1 + i
                cells = list(row) + (list(helper[1][i]) if helper else [])
                out.append(f'<row r="{rr}">' + "".join(
                    _cell(f"{column(c)}{rr}", v, _num_style(v)) for c, v in enumerate(cells))
                    + "</row>")
                fit(cells)
            r += len(block["rows"]) + 2
    return out, placed, widths or [8], scales


SHEET_CT = "application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"
DRAWING_CT = "application/vnd.openxmlformats-officedocument.drawing+xml"


def sheet_parts(chart: Mapping[str, Any], sheet: int, first_chart: int, name: str = "Data"
                ) -> tuple[dict[str, Any], dict[str, str], int]:
    """Sheet ``sheet`` of a workbook holding ``chart``'s rows and its native charts
    (numbered from ``first_chart``): (parts, content-type overrides, charts made)."""
    rows, placed, widths, scales = _sheet_rows(chart)
    width = len(widths)
    natives = placed if chart["kind"] in NATIVE else []
    # each note wraps over the width of the rows and their charts (never widening the page)
    right = width + (10 if natives else 0)
    chars = sum(widths) + (10 * 9 if natives else 0)
    notes = [(r, line) for r, line in enumerate(provenance(chart), 1)]
    rows = [f'<row r="{r}" ht="{12 * max(1, math.ceil(len(line) * 0.9 / chars))}" '
            f'customHeight="1">{_cell(f"A{r}", line, 2)}</row>' for r, line in notes] + rows
    merged = "".join(f'<mergeCell ref="A{r}:{column(right - 1)}{r}"/>' for r, _ in notes) \
        if right > 1 else ""
    merges = f'<mergeCells count="{len(notes)}">{merged}</mergeCells>' if merged else ""
    ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    lo, hi = ("".join(f"{c:02X}" for c in end) for end in RAMP)
    cf = "".join(f'<conditionalFormatting sqref="{s}"><cfRule type="colorScale" priority='
                 f'"{i + 1}"><colorScale><cfvo type="min"/><cfvo type="max"/><color '
                 f'rgb="FF{lo}"/><color rgb="FF{hi}"/></colorScale></cfRule>'
                 "</conditionalFormatting>" for i, s in enumerate(scales))
    cols = "<cols>" + "".join(f'<col min="{c + 1}" max="{c + 1}" width="{w}" '
                              'customWidth="1"/>' for c, w in enumerate(widths)) + "</cols>"
    # printed or saved as PDF: landscape, one page wide
    fit = '<sheetPr><pageSetUpPr fitToPage="1"/></sheetPr>'
    page = ('<pageMargins left="0.5" right="0.5" top="0.6" bottom="0.6" header="0.3" '
            'footer="0.3"/><pageSetup orientation="landscape" fitToWidth="1" '
            'fitToHeight="0"/>')
    parts: dict[str, Any] = {f"xl/worksheets/sheet{sheet}.xml": (
        f'{XML}<worksheet {ns} xmlns:r="{NS_R}">{fit}{cols}<sheetData>{"".join(rows)}'
        f'</sheetData>{merges}{cf}{page}' + ('<drawing r:id="rId1"/>' if natives else "")
        + "</worksheet>")}
    overrides = {f"/xl/worksheets/sheet{sheet}.xml": SHEET_CT}
    if not natives:
        return parts, overrides, 0
    anchors, drels = [], []
    col0 = width + 1
    below = 0   # each chart beside its own rows, never over the one before it
    for i, (top, block) in enumerate(natives):
        n, row0 = first_chart + i, max(top - 1, below)
        below = row0 + 20
        parts[f"xl/charts/chart{n}.xml"] = chart_xml(
            chart, block, sheet=name, top=top,
            title=block["title"] if len(natives) > 1 else None)
        overrides[f"/xl/charts/chart{n}.xml"] = CT_CHART
        drels.append((f"rId{i + 1}", "chart", f"../charts/chart{n}.xml"))
        anchors.append(
            f'<xdr:twoCellAnchor><xdr:from><xdr:col>{col0}</xdr:col><xdr:colOff>0'
            f'</xdr:colOff><xdr:row>{row0}</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:from>'
            f'<xdr:to><xdr:col>{col0 + 9}</xdr:col><xdr:colOff>0</xdr:colOff><xdr:row>'
            f'{row0 + 19}</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:to><xdr:graphicFrame '
            f'macro=""><xdr:nvGraphicFramePr><xdr:cNvPr id="{i + 2}" name="Chart {i + 1}"/>'
            '<xdr:cNvGraphicFramePr/></xdr:nvGraphicFramePr><xdr:xfrm><a:off x="0" y="0"/>'
            '<a:ext cx="0" cy="0"/></xdr:xfrm><a:graphic><a:graphicData uri="'
            f'{NS_C}"><c:chart xmlns:c="{NS_C}" r:id="rId{i + 1}"/></a:graphicData>'
            "</a:graphic></xdr:graphicFrame><xdr:clientData/></xdr:twoCellAnchor>")
    parts[f"xl/drawings/drawing{sheet}.xml"] = (
        f'{XML}<xdr:wsDr xmlns:xdr="http://schemas.openxmlformats.org/drawingml/2006/'
        f'spreadsheetDrawing" xmlns:a="{NS_A}" xmlns:r="{NS_R}">{"".join(anchors)}'
        "</xdr:wsDr>")
    parts[f"xl/drawings/_rels/drawing{sheet}.xml.rels"] = rels(drels)
    parts[f"xl/worksheets/_rels/sheet{sheet}.xml.rels"] = rels(
        [("rId1", "drawing", f"../drawings/drawing{sheet}.xml")])
    overrides[f"/xl/drawings/drawing{sheet}.xml"] = DRAWING_CT
    return parts, overrides, len(natives)


def workbook(sheets: Sequence[tuple[str, dict[str, Any], dict[str, str]]],
             styles: str = STYLES) -> bytes:
    """A workbook of ``sheets`` (name, parts, overrides), sheet n being ``sheet{n}.xml``;
    ``styles``: its styles part (one whose first five cell formats are ``STYLES``')."""
    ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    listed = "".join(f'<sheet name="{escape(name[:31])}" sheetId="{i}" r:id="rId{i}"/>'
                     for i, (name, _, _) in enumerate(sheets, 1))
    parts: dict[str, Any] = {
        "_rels/.rels": rels([("rId1", "officeDocument", "xl/workbook.xml")]),
        "xl/workbook.xml": (f'{XML}<workbook {ns} xmlns:r="{NS_R}"><sheets>{listed}'
                            "</sheets></workbook>"),
        "xl/_rels/workbook.xml.rels": rels(
            [(f"rId{i}", "worksheet", f"worksheets/sheet{i}.xml")
             for i in range(1, len(sheets) + 1)]
            + [(f"rId{len(sheets) + 1}", "styles", "styles.xml")]),
        "xl/styles.xml": styles}
    overrides = {"/xl/workbook.xml": "application/vnd.openxmlformats-officedocument."
                 "spreadsheetml.sheet.main+xml",
                 "/xl/styles.xml": "application/vnd.openxmlformats-officedocument."
                 "spreadsheetml.styles+xml"}
    for _, sheet_parts_, sheet_overrides in sheets:
        parts.update(sheet_parts_)
        overrides.update(sheet_overrides)
    return zip_parts({"[Content_Types].xml": content_types(BASE_TYPES, overrides), **parts})


def xlsx(chart: Mapping[str, Any]) -> bytes:
    """The chart's rows on a sheet with a native chart over those cells (one per small
    multiple); a heatmap or a coloured table carries an Excel colour scale on its cells."""
    parts, overrides, _ = sheet_parts(chart, 1, 1)
    return workbook([("Data", parts, overrides)])


def embedded_charts(chart: Mapping[str, Any], prefix: str, start: int = 1
                    ) -> list[tuple[str, dict[str, Any]]]:
    """For a slide or a page: (chart part name, parts) per native chart of ``chart`` (one per
    small multiple), each chart over the embedded workbook ``xlsx(chart)``."""
    if chart["kind"] not in NATIVE:
        return []
    book = xlsx(chart)
    _, placed, _, _ = _sheet_rows(chart)
    out = []
    for i, (top, block) in enumerate(placed, start):
        name = f"{prefix}/charts/chart{i}.xml"
        out.append((name, {
            name: chart_xml(chart, block, top=top, embedded=True,
                            title=block["title"] if len(placed) > 1 else ""),
            f"{prefix}/charts/_rels/chart{i}.xml.rels": rels(
                [("rId1", "package", f"../embeddings/Workbook{i}.xlsx")]),
            f"{prefix}/embeddings/Workbook{i}.xlsx": book}))
    return out
