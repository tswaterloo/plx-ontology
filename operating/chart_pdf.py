"""A drawn chart (``operator_api.charts``) on a PDF page with reportlab (the optional PDF
dependency ``instruct_files`` already uses): vector bars, lines, areas, slices, points and
coloured cells in the one palette, value axis ticks, a legend, and the chart's source note
(``chart_ooxml.provenance``). Evidence: isolated-fixture."""

from __future__ import annotations

import math
from typing import Any, Mapping, Optional, Sequence

from operating import chart_ooxml as ox

HEIGHT = 210
TEXT, MUTED, GRID = "#1F2937", "#6B7280", "#E5E7EB"


def _hex(rgb: str) -> str:
    return "#" + rgb


COLOURS = [_hex(c) for c in ox.PALETTE]


NOTE_SIZE = 7
NOTE_LEAD = 10


def notes(chart: Mapping[str, Any], width: float = 512) -> list[str]:
    """The chart's provenance lines wrapped to ``width`` points (never cut: the statement
    is the evidence of what the chart reads)."""
    from reportlab.lib.utils import simpleSplit
    return [part for line in ox.provenance(chart)
            for part in simpleSplit(line, "Helvetica", NOTE_SIZE, width) or [""]]


def ticks(lo: float, hi: float, count: int = 5) -> list[float]:
    """Round axis ticks spanning ``lo``..``hi`` (1, 2 or 5 times a power of ten apart)."""
    if hi <= lo:
        hi = lo + 1
    raw = (hi - lo) / max(count - 1, 1)
    power = 10 ** math.floor(math.log10(raw))
    step = next(m * power for m in (1, 2, 5, 10) if m * power >= raw)
    start = math.floor(lo / step) * step
    out, v = [round(start, 10)], start
    while out[-1] < hi - step * 1e-9 and len(out) < 14:   # the last tick covers hi
        v += step
        out.append(round(v, 10))
    return out


def short(v: float) -> str:
    a = abs(v)
    if a >= 1e9:
        return f"{v / 1e9:.1f}B"
    if a >= 1e6:
        return f"{v / 1e6:.1f}M"
    if a >= 1e4:
        return f"{v / 1e3:.0f}k"
    return f"{v:.0f}" if v == int(v) else f"{v:.2f}".rstrip("0").rstrip(".")


def _clip(text: str, n: int) -> str:
    return text if len(text) <= n else text[:n - 1] + "…"


def draw(pdf: Any, chart: Mapping[str, Any], x: float, top: float, width: float, *,
         page_top: float = 750, bottom: float = 50, with_notes: bool = True) -> float:
    """``chart`` drawn below ``top`` (its title, the chart, its notes, which go on over a
    new page starting at ``page_top`` when they reach ``bottom``; ``with_notes`` False:
    the caller sets the notes itself); the y below it."""
    pdf.setFillColor(TEXT)
    pdf.setFont("Helvetica-Bold", 11)
    pdf.drawString(x, top - 12, _clip(str(chart.get("title") or ""), 90))
    area_top = top - 24
    kind = chart["kind"]
    if chart.get("panels"):
        _panels(pdf, chart, x, area_top, width)
    elif kind in ("pie", "donut"):
        _pie(pdf, chart, x, area_top, width)
    elif kind == "scatter":
        _scatter(pdf, chart, x, area_top, width)
    elif kind in ox.TABLES:
        _cells(pdf, chart, x, area_top, width)
    else:
        _cartesian(pdf, chart, chart.get("labels") or [], chart.get("series") or [],
                   x, area_top - HEIGHT, width, HEIGHT, legend=True)
    y = area_top - HEIGHT - 12
    for line in notes(chart, width) if with_notes else ():
        if y < bottom:   # a long statement goes on over the next page
            pdf.showPage()
            y = page_top
        pdf.setFont("Helvetica", NOTE_SIZE)
        pdf.setFillColor(MUTED)
        pdf.drawString(x, y, line)
        y -= NOTE_LEAD
    return y - 14


def _values(series: Sequence[Mapping[str, Any]]) -> list[float]:
    return [v for s in series for v in s["values"] if v is not None]


def _cartesian(pdf: Any, chart: Mapping[str, Any], labels: Sequence[str],
               series: Sequence[Mapping[str, Any]], x0: float, y0: float, w: float, h: float,
               *, legend: bool, scale: Optional[tuple[float, float]] = None) -> None:
    kind = chart["kind"]
    spec = chart.get("spec") or {}
    stacked = bool(spec.get("stacked"))
    horizontal = bool(spec.get("horizontal")) or kind == "funnel"
    left = [s for s in series if s.get("axis") != "right"]
    right = [s for s in series if s.get("axis") == "right"]
    n = max(len(labels), 1)
    if kind == "waterfall":
        run, bars = 0.0, []
        for v in left[0]["values"]:
            v = v or 0.0
            bars.append((run, run + v))
            run += v
        bars.append((0.0, run))
        labels = list(labels) + ["Total"]
        n = len(labels)
        lo, hi = min(0.0, *[min(b) for b in bars]), max(0.0, *[max(b) for b in bars])
    elif stacked:
        pos = [sum(max(s["values"][i] or 0, 0) for s in left) for i in range(len(labels))]
        neg = [sum(min(s["values"][i] or 0, 0) for s in left) for i in range(len(labels))]
        lo, hi = min([0.0, *neg]), max([0.0, *pos])
    else:
        vals = _values(left) or [0.0]
        lo, hi = min(0.0, *vals), max(0.0, *vals)
    if scale:
        lo, hi = scale
    axis = ticks(lo, hi)
    lo, hi = axis[0], axis[-1]
    pad_l = 34 if not horizontal else min(110, 6 + 4.6 * max((len(str(t)) for t in labels),
                                                               default=4))
    pad_b = 30 if not horizontal else 14
    pad_r = 30 if right else 6
    pw, ph = w - pad_l - pad_r, h - pad_b - (14 if legend and len(series) > 1 else 4)
    px, py = x0 + pad_l, y0 + pad_b
    span = (hi - lo) or 1.0

    def val(v: float) -> float:   # value -> position along the value axis
        return (v - lo) / span * (pw if horizontal else ph)

    pdf.setFont("Helvetica", 6.5)
    pdf.setLineWidth(0.4)
    for t in axis:
        pdf.setStrokeColor(GRID)
        pdf.setFillColor(MUTED)
        if horizontal:
            pdf.line(px + val(t), py, px + val(t), py + ph)
            pdf.drawCentredString(px + val(t), py - 9, short(t))
        else:
            pdf.line(px, py + val(t), px + pw, py + val(t))
            pdf.drawRightString(px - 3, py + val(t) - 2, short(t))
    band = (ph if horizontal else pw) / n
    every = max(1, math.ceil(n / (12 if horizontal else 16)))
    pdf.setFillColor(MUTED)
    for i, label in enumerate(labels):
        if i % every:
            continue
        if horizontal:
            pdf.drawRightString(px - 3, py + ph - band * (i + 0.5) - 2, _clip(str(label), 26))
        else:
            pdf.saveState()
            pdf.translate(px + band * (i + 0.5), py - 4)
            pdf.rotate(35 if n > 6 else 0)
            (pdf.drawRightString if n > 6 else pdf.drawCentredString)(0, -6, _clip(str(label), 16))
            pdf.restoreState()
    zero = val(max(lo, min(0.0, hi)))

    def bar(i: int, a: float, b: float, colour: str, slot: int = 0, slots: int = 1) -> None:
        pdf.setFillColor(colour)
        inner = band * 0.78
        thick = inner / slots
        start = band * i + band * 0.11 + thick * slot
        p, q = sorted((val(a), val(b)))
        if horizontal:
            pdf.rect(px + p, py + ph - start - thick, max(q - p, 0.4), thick * 0.92, stroke=0,
                     fill=1)
        else:
            pdf.rect(px + start, py + p, thick * 0.92, max(q - p, 0.4), stroke=0, fill=1)

    if kind == "waterfall":
        for i, (a, b) in enumerate(bars):
            colour = _hex(ox.TOTAL) if i == len(bars) - 1 else _hex(ox.UP if b >= a else ox.DOWN)
            bar(i, a, b, colour)
    elif kind in ("line", "area"):
        for si, s in enumerate(left):
            _line(pdf, s["values"], px, py, band, val, COLOURS[si % len(COLOURS)],
                  fill_to=zero if kind == "area" else None)
    else:
        base_pos = [0.0] * n
        base_neg = [0.0] * n
        for si, s in enumerate(left):
            colour = COLOURS[si % len(COLOURS)]
            for i, v in enumerate(s["values"]):
                if v is None:
                    continue
                if stacked:
                    base = base_pos if v >= 0 else base_neg
                    bar(i, base[i], base[i] + v, colour)
                    base[i] += v
                else:
                    bar(i, 0.0, v, colour, si, len(left))
    if legend and len(left) == 1 and not right:   # no legend: the value axis says what it is
        pdf.setFont("Helvetica", 6.5)
        pdf.setFillColor(MUTED)
        pdf.drawString(px, py + ph + 3, _clip(str(left[0]["name"]).replace("_", " "), 60))
    if right:   # a combo's lines on their own right-hand axis
        rv = _values(right) or [0.0]
        raxis = ticks(min(0.0, *rv), max(0.0, *rv))
        rspan = (raxis[-1] - raxis[0]) or 1.0
        pdf.setFillColor(MUTED)
        for t in raxis:
            pdf.drawString(px + pw + 3, py + (t - raxis[0]) / rspan * ph - 2, short(t))
        for si, s in enumerate(right, len(left)):
            _line(pdf, s["values"], px, py, band,
                  lambda v: (v - raxis[0]) / rspan * ph, COLOURS[si % len(COLOURS)])
    if legend and len(series) > 1:
        lx = px
        for si, s in enumerate(series):
            pdf.setFillColor(COLOURS[si % len(COLOURS)])
            pdf.rect(lx, y0 + h - 8, 6, 6, stroke=0, fill=1)
            pdf.setFillColor(TEXT)
            name = _clip(str(s["name"]).replace("_", " "), 22)
            pdf.drawString(lx + 8, y0 + h - 7.5, name)
            lx += 16 + pdf.stringWidth(name, "Helvetica", 6.5)


def _line(pdf: Any, values: Sequence[Optional[float]], px: float, py: float, band: float,
          val: Any, colour: str, fill_to: Optional[float] = None) -> None:
    pts = [(px + band * (i + 0.5), py + val(v)) for i, v in enumerate(values) if v is not None]
    if not pts:
        return
    if fill_to is not None and len(pts) > 1:
        path = pdf.beginPath()
        path.moveTo(pts[0][0], py + fill_to)
        for p in pts:
            path.lineTo(*p)
        path.lineTo(pts[-1][0], py + fill_to)
        path.close()
        pdf.setFillColor(colour, alpha=0.25)
        pdf.drawPath(path, stroke=0, fill=1)
        pdf.setFillAlpha(1)
    pdf.setStrokeColor(colour)
    pdf.setLineWidth(1.4)
    path = pdf.beginPath()
    path.moveTo(*pts[0])
    for p in pts[1:]:
        path.lineTo(*p)
    pdf.drawPath(path, stroke=1, fill=0)
    pdf.setFillColor(colour)
    for p in pts:
        pdf.circle(p[0], p[1], 1.4, stroke=0, fill=1)


def _pie(pdf: Any, chart: Mapping[str, Any], x: float, top: float, width: float) -> None:
    values = [max(v or 0.0, 0.0) for v in chart["series"][0]["values"]]
    total = sum(values) or 1.0
    r = HEIGHT / 2 - 8
    cx, cy = x + r + 10, top - HEIGHT / 2
    angle = 90.0
    for i, v in enumerate(values):
        extent = -v / total * 360
        pdf.setFillColor(COLOURS[i % len(COLOURS)])
        pdf.setStrokeColor("#FFFFFF")
        pdf.wedge(cx - r, cy - r, cx + r, cy + r, angle, extent, stroke=1, fill=1)
        angle += extent
    if chart["kind"] == "donut":
        pdf.setFillColor("#FFFFFF")
        pdf.circle(cx, cy, r * 0.55, stroke=0, fill=1)
        pdf.setFillColor(TEXT)
        pdf.setFont("Helvetica-Bold", 10)
        pdf.drawCentredString(cx, cy - 3, short(total))
    pdf.setFont("Helvetica", 7.5)
    for i, label in enumerate(chart["labels"]):
        y = top - 14 - i * 13
        pdf.setFillColor(COLOURS[i % len(COLOURS)])
        pdf.rect(cx + r + 24, y, 7, 7, stroke=0, fill=1)
        pdf.setFillColor(TEXT)
        pdf.drawString(cx + r + 36, y + 0.5, f"{_clip(str(label), 34)}  {values[i] / total:.0%}"
                       f"  ({ox.fmt(values[i])})")


def _scatter(pdf: Any, chart: Mapping[str, Any], x: float, top: float, width: float) -> None:
    xs = [v for v in chart.get("xs") or [] if v is not None]
    ys = _values(chart["series"])
    if not xs or not ys:
        return
    xa, ya = ticks(min(xs), max(xs)), ticks(min(0.0, *ys), max(ys))
    px, py, pw, ph = x + 34, top - HEIGHT + 22, width - 40, HEIGHT - 28

    def X(v: float) -> float:
        return px + (v - xa[0]) / ((xa[-1] - xa[0]) or 1) * pw

    def Y(v: float) -> float:
        return py + (v - ya[0]) / ((ya[-1] - ya[0]) or 1) * ph
    pdf.setFont("Helvetica", 6.5)
    pdf.setLineWidth(0.4)
    for t in ya:
        pdf.setStrokeColor(GRID)
        pdf.line(px, Y(t), px + pw, Y(t))
        pdf.setFillColor(MUTED)
        pdf.drawRightString(px - 3, Y(t) - 2, short(t))
    for t in xa:
        pdf.drawCentredString(X(t), py - 10, short(t))
    # what each axis is: x under its numbers, y over the plot
    pdf.drawRightString(px + pw, py - 19, _clip(str(chart.get("x") or "").replace("_", " "), 60))
    if len(chart["series"]) == 1:
        pdf.drawString(px, py + ph + 3, _clip(str(chart["series"][0]["name"]).replace("_", " "),
                                              60))
    for si, s in enumerate(chart["series"]):
        pdf.setFillColor(COLOURS[si % len(COLOURS)], alpha=0.8)
        for xv, yv in zip(chart.get("xs") or [], s["values"]):
            if xv is not None and yv is not None:
                pdf.circle(X(xv), Y(yv), 2.2, stroke=0, fill=1)
    pdf.setFillAlpha(1)
    trend = chart.get("trend")
    if trend:
        pdf.setStrokeColor(_hex(ox.TOTAL))
        pdf.setDash(3, 2)
        pdf.line(X(xa[0]), Y(trend["slope"] * xa[0] + trend["intercept"]),
                 X(xa[-1]), Y(trend["slope"] * xa[-1] + trend["intercept"]))
        pdf.setDash()


def _cells(pdf: Any, chart: Mapping[str, Any], x: float, top: float, width: float) -> None:
    from operating.chart_docs import _grid
    header, rows, fills = _grid(chart)
    rows = rows[:16]
    cols = max(len(header), 1)
    cw = width / cols
    rh = min(16.0, (HEIGHT - 4) / (len(rows) + 1))
    pdf.setFont("Helvetica-Bold", 7)
    for c, h in enumerate(header):
        pdf.setFillColor(TEXT)
        pdf.drawString(x + c * cw + 3, top - rh + 4, _clip(str(h).replace("_", " "), 18))
    big = chart["kind"] == "kpi"
    for r, row in enumerate(rows):
        y = top - rh * (r + 2)
        for c, text in enumerate(row):
            fill = fills[r][c] if r < len(fills) and c < len(fills[r]) else None
            if fill:
                pdf.setFillColor(_hex(fill))
                pdf.rect(x + c * cw, y, cw - 1, rh - 1, stroke=0, fill=1)
            pdf.setFillColor(_hex(ox.ink(fill)))
            pdf.setFont("Helvetica-Bold" if big and r == 0 else "Helvetica",
                        12 if big and r == 0 else 7)
            pdf.drawString(x + c * cw + 3, y + 4,
                           _clip(str(text), 30 if big and r == 0 else max(18, int(cw / 3.6))))


def _panels(pdf: Any, chart: Mapping[str, Any], x: float, top: float, width: float) -> None:
    panels = chart["panels"]
    cols = min(3, len(panels))
    rows = math.ceil(len(panels) / cols)
    w, h = width / cols, HEIGHT / rows
    vals = [v for p in panels for s in p["series"] for v in s["values"] if v is not None]
    scale = (min(0.0, *vals), max(0.0, *vals)) if vals else (0.0, 1.0)
    for i, p in enumerate(panels):
        px, py = x + w * (i % cols), top - h * (i // cols + 1)
        pdf.setFillColor(TEXT)
        pdf.setFont("Helvetica-Bold", 7.5)
        pdf.drawString(px + 34, py + h - 9, _clip(str(p["title"]), 30))
        _cartesian(pdf, chart, p["labels"], p["series"], px, py, w - 6, h - 12, legend=False,
                   scale=scale)
