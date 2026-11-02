// A small, safe reading of the markdown the assistant writes: paragraphs, "- " / "* " /
// "1. " bullets and **bold**. It returns plain data (never HTML); components render it as
// React text, so nothing in an answer can inject markup.

export interface Inline {
  text: string;
  bold?: boolean;
}

export type Block = { kind: "p"; inlines: Inline[] } | { kind: "ul"; items: Inline[][] };

/** "a **b** c" -> [{a }, {b, bold}, { c}]; an unpaired ** is dropped, never shown. */
export function inlines(text: string): Inline[] {
  const out: Inline[] = [];
  const parts = text.split(/(\*\*[^*]+?\*\*|__[^_]+?__)/g);
  for (const part of parts) {
    if (!part) continue;
    const m = /^(\*\*|__)([\s\S]+)\1$/.exec(part);
    if (m) out.push({ text: m[2], bold: true });
    else out.push({ text: part.replace(/\*\*|(^|\s)__|__(?=\s|$)/g, "$1").replace(/(^|\s)[*_](\S[^*_]*?\S|\S)[*_](?=[\s.,;:!?)]|$)/g, "$1$2") });
  }
  // a run of the same weight reads as one
  return out.reduce<Inline[]>((acc, x) => {
    const last = acc[acc.length - 1];
    if (last && !!last.bold === !!x.bold) last.text += x.text;
    else acc.push({ ...x });
    return acc;
  }, []);
}

const BULLET = /^\s*(?:[-*•]|\d{1,2}[.)])\s+/;
const HEADING = /^\s{0,3}#{1,6}\s+/;

/** Blank-line paragraphs and bullet lists; a "# heading" line reads as a bold paragraph. */
export function parseMarkdown(text: string | null | undefined): Block[] {
  if (!text) return [];
  const blocks: Block[] = [];
  let para: string[] = [];
  let list: Inline[][] | null = null;
  const flushPara = () => {
    if (para.length) blocks.push({ kind: "p", inlines: inlines(para.join(" ").replace(/\s+/g, " ").trim()) });
    para = [];
  };
  const flushList = () => {
    if (list && list.length) blocks.push({ kind: "ul", items: list });
    list = null;
  };
  for (const raw of text.replace(/\r\n?/g, "\n").split("\n")) {
    const line = raw.trimEnd();
    if (!line.trim()) {
      flushPara();
      flushList();
      continue;
    }
    if (BULLET.test(line)) {
      flushPara();
      (list ??= []).push(inlines(line.replace(BULLET, "").trim()));
      continue;
    }
    if (HEADING.test(line)) {
      flushPara();
      flushList();
      blocks.push({ kind: "p", inlines: [{ text: line.replace(HEADING, "").replace(/\*\*/g, "").trim(), bold: true }] });
      continue;
    }
    if (list) {
      // a wrapped bullet line continues its item
      const items: Inline[][] = list;
      const last = items[items.length - 1];
      items[items.length - 1] = inlines(last.map((i) => (i.bold ? `**${i.text}**` : i.text)).join("") + " " + line.trim());
      continue;
    }
    para.push(line.trim());
  }
  flushPara();
  flushList();
  return blocks.filter((b) => (b.kind === "p" ? b.inlines.some((i) => i.text.trim()) : b.items.length > 0));
}

/** The plain text of inlines (for tests and aria). */
export function plainText(xs: Inline[]): string {
  return xs.map((x) => x.text).join("");
}

/** "Label: value" lines only (2 or more): a field dump, not an answer. */
export function fieldPairs(text: string | null | undefined): [string, string][] | null {
  if (!text) return null;
  const lines = text
    .split("\n")
    .map((l) => l.replace(BULLET, "").replace(/\*\*/g, "").trim())
    .filter(Boolean);
  if (lines.length < 2) return null;
  const pairs: [string, string][] = [];
  for (const l of lines) {
    const m = /^([A-Za-z][\w ()/.-]{0,40}):\s+(.{1,80})$/.exec(l);
    if (!m || /[.!?]\s+\S/.test(m[2])) return null;
    pairs.push([m[1].trim(), m[2].trim()]);
  }
  return pairs;
}
