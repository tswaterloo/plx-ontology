// The app's looks stay plain: dense rows, line icons, modest radius and flashes of the blue
// accent, with nothing decorative. This reads every component and stylesheet under src
// (tests left out) and fails on gradients, glows, looping attention motion, pill badges and
// emoji. Data marks are allowed: a repeating-linear-gradient hatch draws a value, and
// animate-spin is the busy spinner.
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, relative } from "node:path";
import { describe, expect, it } from "vitest";

const SRC = join(__dirname, "..");

function files(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) return files(path);
    return /\.(tsx|ts|css)$/.test(name) && !/\.test\.tsx?$/.test(name) && !/\.d\.ts$/.test(name) ? [path] : [];
  });
}

const RULES: [string, RegExp][] = [
  ["gradient", /\bbg-(?:gradient|linear|radial|conic)-|(?<!repeating-)(?:linear|radial|conic)-gradient\(/],
  ["glow", /\b0_0_0_(?:[2-9]|\d{2,})px|(?<![\w])0_0_[1-9]\d*px_[^\]\s]*--accent|\bdrop-shadow(?:-|\b)|\bshadow-(?:accent|blue|\w+-\d{3})\b/],
  ["looping motion", /\banimate-(?:pulse|ping|bounce|blink)\b|(?<!skeleton-pulse[^;]*)\binfinite\b(?![^;]*\bspin\b)/],   // a loading placeholder's slow pulse (owner 11-24) is the one allowed loop
  ["pill badge", /\brounded-full\b[^"'`]*\b(?:px|py)-\d|\b(?:px|py)-\d[^"'`]*\brounded-full\b/],
  ["emoji", /[\u{1F300}-\u{1FAFF}\u{2600}-\u{27BF}\u{2B50}\u{2B55}\u{FE0F}]/u],
];

/** The style tells in one source text, each "name: match". Comments are left out. */
export function styleTells(source: string): string[] {
  const code = source.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");
  const found: string[] = [];
  for (const [name, re] of RULES) {
    const m = code.match(re);
    if (m) found.push(`${name}: ${m[0]}`);
  }
  return found;
}

describe("app style", () => {
  it("the rules catch decoration and pass data marks", () => {
    expect(styleTells(`<div className="bg-gradient-to-t from-bg" />`)).toEqual(["gradient: bg-gradient-"]);
    expect(styleTells(`"[mask-image:linear-gradient(to_right,#000,transparent)]"`)).toEqual(["gradient: linear-gradient("]);
    expect(styleTells(`"focus-within:shadow-[0_0_0_4px_color-mix(in_srgb,var(--accent)_12%,transparent)]"`)).toEqual(["glow: 0_0_0_4px"]);
    expect(styleTells(`<span className="size-2 animate-pulse" />`)).toEqual(["looping motion: animate-pulse"]);
    expect(styleTells(`<span className="rounded-full bg-accent-soft px-2 text-[11px]">New</span>`)[0]).toMatch(/^pill badge/);
    expect(styleTells(`<span>Done ✅</span>`)).toEqual(["emoji: ✅"]);
    expect(styleTells(`"repeating-linear-gradient(135deg,var(--late) 0 2px,transparent 2px 5px)"`)).toEqual([]);
    expect(styleTells(`<span className="size-3 animate-spin rounded-full border" />`)).toEqual([]);
    expect(styleTells(`<button className="focus-within:shadow-[0_0_0_1px_var(--accent)] rounded" />`)).toEqual([]);
    expect(styleTells(`<span className="size-1.5 rounded-full bg-accent" />`)).toEqual([]);
  });

  it("no component or stylesheet carries a decorative tell", () => {
    const bad: string[] = [];
    for (const file of files(SRC)) {
      for (const t of styleTells(readFileSync(file, "utf8"))) bad.push(`${relative(SRC, file)}: ${t}`);
    }
    expect(bad).toEqual([]);
  });
});
