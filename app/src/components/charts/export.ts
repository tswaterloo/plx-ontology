// Saving a chart: a PNG of what is on screen (drawn from the chart's SVG at twice its size)
// and the Office/PDF files the server makes from the chart (lib/api chartFile).

import { chartFile } from "@/lib/api";

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
export type PngWords = { items: { name: string; colour: string }[]; caption: string; notes: string[]; muted: string };

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

/** The chart's SVG as a PNG on its theme's ground: its title and legend above, its notes
 * under it. */
export async function savePng(svg: SVGSVGElement, title: string, ground: string, ink: string, words?: PngWords): Promise<void> {
  const box = svg.viewBox.baseVal;
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
  const clone = svg.cloneNode(true) as SVGSVGElement;
  clone.setAttribute("xmlns", "http://www.w3.org/2000/svg");
  clone.setAttribute("width", String(box.width));
  clone.setAttribute("height", String(box.height));
  clone.querySelectorAll("[data-hover]").forEach((n) => n.remove());
  const data = new XMLSerializer().serializeToString(clone);
  const img = new Image();
  // a data: URL: the app's CSP lets images load from 'self' and data: only (no blob:)
  const url = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(data)}`;
  await new Promise<void>((ok, fail) => {
    img.onload = () => ok();
    img.onerror = () => fail(new Error("chart image"));
    img.src = url;
  });
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
  ctx.drawImage(img, 16, head + 12, box.width, box.height);
  ctx.fillStyle = words?.muted ?? ink;
  notes.forEach((line, i) => ctx.fillText(line, 16, head + 12 + box.height + 16 + i * 16));
  const blob = await new Promise<Blob | null>((ok) => canvas.toBlob(ok, "image/png"));
  if (blob) save(blob, fileName(title, "png"));
}

/** A chart as a server-made file, saved under its title. */
export async function saveFile(chart: { title: string }, format: (typeof FILES)[number]["format"]): Promise<void> {
  save(await chartFile(chart, format), fileName(chart.title, format));
}
