"""Excel (.xlsx) export of any chart, table or answer (Genie item 15), standard library only.

Reuses ``operating.chart_ooxml`` (native Excel charts + workbook assembly) and
``operating.doc_render`` (typed numbers, dates, money, frozen filtered headers). Every
exported text, cell, chart and SQL statement is masked of card numbers, secrets and chat
doubt marks before writing; every cited table, chart or figure keeps its full uncut SQL
statement on a ``Sources`` worksheet."""
from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

from operating import chart_ooxml as ox
from operating import doc_render as dr
from operating import instruct_files as files
from operating.card_text import mask_served

_BAD_SHEET = re.compile(r"[\[\]:*?/\\]+")
_MAX_SHEET_NAME = 31


def sheet_name(raw: Any, fallback: str, used: set[str]) -> str:
    """A valid, unique Excel worksheet tab name (at most 31 characters)."""
    cleaned = re.sub(r"\s+", " ", _BAD_SHEET.sub(" ", str(raw or ""))).strip(" '")
    base = cleaned[:_MAX_SHEET_NAME].strip() or fallback   # guard-ok: Excel sheet tab name limit is 31 chars
    name = base
    n = 2
    while name.lower() in used:
        suffix = f" ({n})"
        name = base[: _MAX_SHEET_NAME - len(suffix)].strip() + suffix   # guard-ok: Excel sheet tab name limit is 31 chars
        n += 1
    used.add(name.lower())
    return name


def _clean_text(val: Any) -> str:
    return dr.without_doubt_marks(mask_served(str(val or "")))


def _check_chart_shape(chart: Any) -> None:
    if not isinstance(chart, Mapping):
        raise ValueError("each chart must be an object")
    kind = chart.get("kind")
    if kind not in (ox.NATIVE | ox.TABLES):
        raise ValueError("That is not a chart the Workbench draws.")
    labels = chart.get("labels")
    if labels is None:
        labels = []
    if not isinstance(labels, Sequence) or isinstance(labels, (str, bytes)):
        raise ValueError("chart labels must be a list")
    series = chart.get("series")
    if series is None:
        series = []
    if not isinstance(series, Sequence) or isinstance(series, (str, bytes)):
        raise ValueError("chart series must be a list")
    panels = chart.get("panels")
    if panels is not None:
        if not isinstance(panels, Sequence) or isinstance(panels, (str, bytes)):
            raise ValueError("chart panels must be a list")
        for panel in panels:
            if not isinstance(panel, Mapping):
                raise ValueError("each chart panel must be an object")
            p_labels = panel.get("labels") or []
            p_series = panel.get("series") or []
            if not isinstance(p_labels, Sequence) or isinstance(p_labels, (str, bytes)) \
                    or not isinstance(p_series, Sequence) or isinstance(p_series, (str, bytes)):
                raise ValueError("chart panel labels and series must be lists")
            for s in p_series:
                if not isinstance(s, Mapping):
                    raise ValueError("each chart series must be an object")
                vals = s.get("values") or ()
                if not isinstance(vals, Sequence) or isinstance(vals, (str, bytes)) \
                        or len(vals) != len(p_labels):
                    raise ValueError("That chart's values do not line up with its labels.")
    for s in series:
        if not isinstance(s, Mapping):
            raise ValueError("each chart series must be an object")
        vals = s.get("values") or ()
        if not isinstance(vals, Sequence) or isinstance(vals, (str, bytes)) \
                or len(vals) != len(labels):
            raise ValueError("That chart's values do not line up with its labels.")
    xs = chart.get("xs")
    if xs is not None:
        if not isinstance(xs, Sequence) or isinstance(xs, (str, bytes)) or len(xs) != len(labels):
            raise ValueError("That chart's values do not line up with its labels.")
    if kind == "waterfall" and labels and not panels and not series:
        raise ValueError("That chart's values do not line up with its labels.")


def _table_preview_note(t: Mapping[str, Any]) -> str | None:
    shown = len(t.get("rows") or ())
    total = t.get("total_rows")
    if total is None:
        total = t.get("records")
    truncated = bool(t.get("truncated")) or (total is not None and shown < total)
    if not truncated:
        return None
    if total is not None:
        return f"{shown} of {total} rows shown (preview)"
    return f"{shown} rows shown (preview; more not shown)"


def _chart_records_label(c: Mapping[str, Any]) -> str:
    rows = c.get("rows")
    rows_total = c.get("rows_total")
    partial = bool(c.get("partial")) or (
        rows is not None and rows_total is not None and rows < rows_total
    )
    if partial and rows is not None and rows_total is not None:
        return f"{rows} of {rows_total} rows shown (preview)"
    if partial and rows is not None:
        return f"{rows} rows shown (preview; more not shown)"
    shown = c.get("shown")
    if shown is not None and rows is not None and shown < rows:
        return f"{shown} of {rows} rows shown"
    return str(rows if rows is not None else len(c.get("labels") or ()))


def _table_sheet_rows(t: Mapping[str, Any]) -> list[list[str]]:
    title = _clean_text(t.get("title") or "").strip()
    cols = [_clean_text(c).strip() for c in (t.get("columns") or ())]
    rows = [[_clean_text(c).strip() for c in r] for r in (t.get("rows") or ())]
    out: list[list[str]] = []
    if title:
        out.append([title])
    if cols:
        out.append(cols)
    out.extend(rows)
    note = _table_preview_note(t)
    if note:
        out.append([""])
        out.append([f"Note: {note}."])
    return out


def _sources_rows(tables: Sequence[Mapping[str, Any]], charts: Sequence[Mapping[str, Any]],
                  citations: Sequence[Mapping[str, Any]]) -> list[list[str]]:
    rows: list[list[str]] = [["Item", "Records", "As of", "Statement"]]
    for idx, t in enumerate(tables, 1):
        sql = " ".join(_clean_text(t.get("sql") or "").split())
        as_of = str(t.get("as_of") or "")[:10]   # guard-ok: ISO date prefix YYYY-MM-DD
        preview_note = _table_preview_note(t)
        if sql or as_of or preview_note:
            rows.append([
                _clean_text(t.get("title") or f"Table {idx}"),
                preview_note or str(len(t.get("rows") or ())),
                as_of,
                sql,
            ])
    for idx, c in enumerate(charts, 1):
        sql = " ".join(_clean_text(c.get("sql") or "").split())
        as_of = str(c.get("as_of") or "")[:10]   # guard-ok: ISO date prefix YYYY-MM-DD
        partial = bool(c.get("partial")) or (
            c.get("rows") is not None and c.get("rows_total") is not None
            and c["rows"] < c["rows_total"]
        )
        if sql or as_of or partial:
            rows.append([
                _clean_text(c.get("title") or f"Chart {idx}"),
                _chart_records_label(c),
                as_of,
                sql,
            ])
    for idx, cite in enumerate(citations, 1):
        sql = " ".join(_clean_text(cite.get("sql") or "").split())
        as_of = str(cite.get("as_of") or "")[:10]   # guard-ok: ISO date prefix YYYY-MM-DD
        if sql or as_of:
            rows.append([
                _clean_text(cite.get("label") or cite.get("title") or f"Figure {idx}"),
                str(cite.get("count") if cite.get("count") is not None else ""),
                as_of,
                sql,
            ])
    return rows


def workbook_xlsx(*, title: str = "Export", summary: str = "",
                  tables: Sequence[Mapping[str, Any]] = (),
                  charts: Sequence[Mapping[str, Any]] = (),
                  citations: Sequence[Mapping[str, Any]] = ()) -> bytes:
    """Build one `.xlsx` workbook holding any combination of answer summary, tables, live
    charts and a ``Sources`` sheet with every full uncut SQL statement."""
    clean_title = _clean_text(title).strip() or "Export"
    clean_summary = _clean_text(summary).strip()
    clean_tables = [t for t in tables if isinstance(t, Mapping)
                    and (t.get("columns") or t.get("rows"))]
    clean_charts: list[dict[str, Any]] = []
    for c in charts:
        _check_chart_shape(c)
        clean_charts.append(files._masked(c))
    clean_cites = [c for c in citations if isinstance(c, Mapping) and (c.get("sql") or c.get("as_of"))]

    if not clean_summary and not clean_tables and not clean_charts:
        raise ValueError("nothing to export: provide a chart, a table or an answer")

    # Single chart with no summary/tables/extra citations -> delegate directly to native chart workbook
    if len(clean_charts) == 1 and not clean_summary and not clean_tables and not clean_cites:
        return ox.xlsx(clean_charts[0])

    sheets: list[tuple[str, dict[str, Any], dict[str, str]]] = []
    used_names: set[str] = set()
    next_chart_id = 1

    if clean_summary:
        ans_rows: list[list[str]] = [[clean_title], [""]]
        for ln in clean_summary.splitlines():
            stripped = ln.strip()
            if stripped:
                ans_rows.append([dr.plain(stripped)])
        idx = len(sheets) + 1
        xml = dr.xlsx_sheet(ans_rows)
        sheets.append((sheet_name("Answer", "Answer", used_names),
                       {f"xl/worksheets/sheet{idx}.xml": xml},
                       {f"/xl/worksheets/sheet{idx}.xml": ox.SHEET_CT}))

    for t_i, tbl in enumerate(clean_tables, 1):
        idx = len(sheets) + 1
        t_rows = _table_sheet_rows(tbl)
        tab = sheet_name(tbl.get("title") or f"Table {t_i}", f"Table {t_i}", used_names)
        xml = dr.xlsx_sheet(t_rows)
        sheets.append((tab, {f"xl/worksheets/sheet{idx}.xml": xml},
                       {f"/xl/worksheets/sheet{idx}.xml": ox.SHEET_CT}))

    for c_i, chart in enumerate(clean_charts, 1):
        idx = len(sheets) + 1
        tab = sheet_name(chart.get("title") or f"Chart {c_i}", f"Chart {c_i}", used_names)
        c_parts, c_types, made = ox.sheet_parts(chart, idx, next_chart_id, tab)
        sheets.append((tab, c_parts, c_types))
        next_chart_id += made

    src_rows = _sources_rows(clean_tables, clean_charts, clean_cites)
    if len(src_rows) > 1:
        idx = len(sheets) + 1
        xml = dr.xlsx_sheet(src_rows, literal=True)
        sheets.append((sheet_name("Sources", "Sources", used_names),
                       {f"xl/worksheets/sheet{idx}.xml": xml},
                       {f"/xl/worksheets/sheet{idx}.xml": ox.SHEET_CT}))

    return ox.workbook(sheets, styles=dr.XLSX_STYLES)
