// Saving a chart: a PNG of what is on screen (drawn from the chart's SVG at twice its size)
// and the Office/PDF files the server makes from the chart (lib/api chartFile).

import { chartFile, exportXlsx } from "@/lib/api";
import type { Answer, ExportXlsxRequest } from "@/lib/types";
import { human } from "./scale";

type ExportChart = NonNullable<Answer["charts"]>[number];

export const FILES = [
  { format: "xlsx", label: "Excel" },
  { format: "pptx", label: "PowerPoint" },
  { format: "docx", label: "Word" },
  { format: "pdf", label: "PDF" },
] as const;

/** Pure: a file name of a chart's title (letters, digits, spaces, dashes). */
export function fileName(title: string, ext: string): string {
  const base = title.replace(/[^A-Za-z0-9 ._()-]+/g, " ").replace(/\s+/g, " ").trim().slice(0, 80) || "Chart";
  return `${base}.${ext}`;
}

function save(blob: Blob, name: string) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

/** What a PNG carries besides the drawing: the legend (or axis caption) above it and the
 * notes (what it is drawn from, what it leaves out) under it, as on screen. */
export type PngWords = { items: { name: string; colour: string }[]; caption: string; notes: string[]; muted: string;
  /** Small multiples: each panel's title, in the order of the drawings. */
  panels?: string[] };

const FONT = "Inter, 'Segoe UI', Helvetica, Arial, sans-serif";

/** Pure: ``text`` broken into lines of at most ``width`` px as ``measure`` measures them. */
export function wrap(text: string, width: number, measure: (s: string) => number): string[] {
  const out: string[] = [];
  let line = "";
  for (const word of text.split(/\s+/).filter(Boolean)) {
    const next = line ? `${line} ${word}` : word;
    if (line && measure(next) > width) {
      out.push(line);
      line = word;
    } else line = next;
  }
  if (line) out.push(line);
  return out;
}

export interface PanelCell { x: number; y: number; width: number; height: number }

/** Pure: where each drawing goes in a PNG: one alone, or small multiples two across as on
 * screen, each panel's title (``titleH`` high) above it. */
export function panelGrid(sizes: { width: number; height: number }[], titleH = 0, gap = 16): { width: number; height: number; cells: PanelCell[] } {
  const cols = sizes.length > 1 ? 2 : 1;
  const colW = Math.max(0, ...sizes.map((s) => s.width));
  const cells: PanelCell[] = [];
  let y = 0;
  for (let i = 0; i < sizes.length; i += cols) {
    const row = sizes.slice(i, i + cols);
    row.forEach((s, k) => cells.push({ x: k * (colW + gap), y: y + titleH, width: s.width, height: s.height }));
    y += titleH + Math.max(...row.map((s) => s.height)) + gap;
  }
  return { width: cols * colW + (cols - 1) * gap, height: Math.max(0, y - gap), cells };
}

function svgImage(svg: SVGSVGElement): Promise<HTMLImageElement> {
  const box = svg.viewBox.baseVal;
  const clone = svg.cloneNode(true) as SVGSVGElement;
  clone.setAttribute("xmlns", "http://www.w3.org/2000/svg");
  clone.setAttribute("width", String(box.width));
  clone.setAttribute("height", String(box.height));
  clone.querySelectorAll("[data-hover]").forEach((n) => n.remove());
  const data = new XMLSerializer().serializeToString(clone);
  const img = new Image();
  // a data: URL: the app's CSP lets images load from 'self' and data: only (no blob:)
  const url = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(data)}`;
  return new Promise<HTMLImageElement>((ok, fail) => {
    img.onload = () => ok(img);
    img.onerror = () => fail(new Error("chart image"));
    img.src = url;
  });
}

/** The chart's SVG (or its small multiples' SVGs, each under its panel title) as a PNG on its
 * theme's ground: its title and legend above, its notes under it. */
export async function savePng(svg: SVGSVGElement | SVGSVGElement[], title: string, ground: string, ink: string, words?: PngWords): Promise<void> {
  const svgs = Array.isArray(svg) ? svg : [svg];
  const titles = words?.panels ?? [];
  const layout = panelGrid(svgs.map((s) => ({ width: s.viewBox.baseVal.width, height: s.viewBox.baseVal.height })), titles.length ? 18 : 0);
  const box = { width: layout.width, height: layout.height };
  const scale = 2;
  const measureCtx = document.createElement("canvas").getContext("2d");
  const measure = (font: string) => (t: string) => {
    if (!measureCtx) return t.length * 7;
    measureCtx.font = font;
    return measureCtx.measureText(t).width;
  };
  const small = `12px ${FONT}`;
  const key = words?.caption ? [words.caption] : [];
  const legend = words?.items ?? [];
  const notes = (words?.notes ?? []).flatMap((n) => wrap(n, box.width, measure(small)));
  const keyH = key.length || legend.length ? 20 : 0;
  const head = (title ? 30 : 0) + keyH;
  const foot = notes.length * 16;
  const images = await Promise.all(svgs.map(svgImage));
  const canvas = document.createElement("canvas");
  canvas.width = (box.width + 32) * scale;
  canvas.height = (box.height + head + foot + 24) * scale;
  const ctx = canvas.getContext("2d");
  if (!ctx) return;
  ctx.scale(scale, scale);
  ctx.fillStyle = ground;
  ctx.fillRect(0, 0, box.width + 32, box.height + head + foot + 24);
  if (title) {
    ctx.fillStyle = ink;
    ctx.font = `600 15px ${FONT}`;
    ctx.fillText(title, 16, 24);
  }
  const keyY = (title ? 30 : 0) + 14;
  ctx.font = small;
  if (key.length) {
    ctx.fillStyle = words?.muted ?? ink;
    ctx.fillText(key[0], 16, keyY);
  }
  let x = 16;
  for (const item of legend) {
    ctx.fillStyle = item.colour;
    ctx.fillRect(x, keyY - 9, 10, 10);
    ctx.fillStyle = words?.muted ?? ink;
    ctx.fillText(item.name, x + 15, keyY);
    x += 15 + measure(small)(item.name) + 16;
  }
  images.forEach((img, i) => {
    const c = layout.cells[i];
    if (titles[i]) {
      ctx.fillStyle = ink;
      ctx.font = `500 12px ${FONT}`;
      ctx.fillText(titles[i], 16 + c.x, head + 12 + c.y - 5);
    }
    ctx.drawImage(img, 16 + c.x, head + 12 + c.y, c.width, c.height);
  });
  ctx.font = small;
  ctx.fillStyle = words?.muted ?? ink;
  notes.forEach((line, i) => ctx.fillText(line, 16, head + 12 + box.height + 16 + i * 16));
  const blob = await new Promise<Blob | null>((ok) => canvas.toBlob(ok, "image/png"));
  if (blob) save(blob, fileName(title, "png"));
}

/** A chart as a server-made file, saved under its title. */
export async function saveFile(chart: { title: string }, format: (typeof FILES)[number]["format"]): Promise<void> {
  save(await chartFile(chart, format), fileName(chart.title, format));
}

/** Any chart, table or answer as a formatted Excel (.xlsx) workbook, saved under its title. */
export async function saveExcel(req: ExportXlsxRequest, title?: string): Promise<void> {
  const name = title || req.title || "Export";
  save(await exportXlsx({ ...req, title: name }), fileName(name, "xlsx"));
}

const FORMULA_LEAD = /^[=+\-@\t\r]/;

/** Pure: one RFC-4180 CSV cell, safe against spreadsheet formula injection on text values. */
export function csvCell(value: string | number | null | undefined): string {
  if (value === null || value === undefined) return "";
  if (typeof value === "number") return Number.isFinite(value) ? String(value) : "";
  const raw = String(value);
  const safe = FORMULA_LEAD.test(raw) && !/^-?\d+(?:\.\d+)?$/.test(raw.trim()) ? `'${raw}` : raw;
  return /[",\r\n]/.test(safe) ? `"${safe.replace(/"/g, '""')}"` : safe;
}

/** Pure: columns and rows formatted as RFC-4180 CSV text. */
export function toCsv(columns: readonly string[], rows: readonly (readonly (string | number | null | undefined)[])[]): string {
  const lines = [columns.map(csvCell).join(",")];
  for (const row of rows) {
    lines.push(columns.map((_, i) => csvCell(row[i])).join(","));
  }
  return `${lines.join("\r\n")}\r\n`;
}

/** Pure: a chart's plotted data (single-panel, scatter, or faceted small multiples) as columns and rows. */
export function chartToTable(chart: ExportChart): { columns: string[]; rows: (string | number | null)[][] } {
  const xLabel = human(chart.x || "category");
  if (chart.panels && chart.panels.length > 0) {
    const facetLabel = human(chart.spec?.facet || "group");
    const seriesNames: string[] = [];
    for (const p of chart.panels) {
      for (const s of p.series) {
        if (!seriesNames.includes(s.name)) seriesNames.push(s.name);
      }
    }
    const columns = [facetLabel, xLabel, ...seriesNames.map((n) => human(n))];
    const rows: (string | number | null)[][] = [];
    for (const p of chart.panels) {
      const byName = new Map(p.series.map((s) => [s.name, s.values]));
      p.labels.forEach((lbl, i) => {
        rows.push([p.title, lbl, ...seriesNames.map((n) => byName.get(n)?.[i] ?? null)]);
      });
    }
    return { columns, rows };
  }
  if (chart.kind === "scatter" && chart.xs && chart.xs.length === chart.labels.length) {
    const labelCol = human(chart.spec?.label || "item");
    const columns = [labelCol, xLabel, ...chart.series.map((s) => human(s.name))];
    const rows = chart.labels.map((lbl, i) => [lbl, chart.xs?.[i] ?? null, ...chart.series.map((s) => s.values[i] ?? null)]);
    return { columns, rows };
  }
  const columns = [xLabel, ...chart.series.map((s) => human(s.name))];
  const rows = chart.labels.map((lbl, i) => [lbl, ...chart.series.map((s) => s.values[i] ?? null)]);
  return { columns, rows };
}

/** Instant client-side CSV download for any table. */
export function saveCsv(columns: readonly string[], rows: readonly (readonly (string | number | null | undefined)[])[], title: string): void {
  const blob = new Blob([toCsv(columns, rows)], { type: "text/csv;charset=utf-8" });
  save(blob, fileName(title || "Export", "csv"));
}

/** Instant client-side CSV download for a drawn chart. */
export function saveChartCsv(chart: ExportChart): void {
  const { columns, rows } = chartToTable(chart);
  saveCsv(columns, rows, chart.title || "Chart");
}
