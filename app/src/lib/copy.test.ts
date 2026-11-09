// The app's own words obey the product copy rules (operating/copy_rules.json, the same file
// the server's slop gate reads): no filler or AI vocabulary, no decoration characters, no
// "(s)" plurals, supplier rather than vendor. It reads every component's visible text
// (JSX text and prose-shaped string literals); tests and generated types are left out.
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, relative } from "node:path";
import { describe, expect, it } from "vitest";

type Rule = { pattern?: string; char?: string; use?: string; reason: string; applies: string[] };
const rules = JSON.parse(readFileSync(join(__dirname, "../../../operating/copy_rules.json"), "utf8")) as {
  banned_phrases: Rule[];
  banned_chars: Rule[];
  terms: Rule[];
};
const SRC = join(__dirname, "..");

function files(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) return files(path);
    return /\.tsx$/.test(name) && !/\.test\.tsx$/.test(name) ? [path] : [];
  });
}

/** The visible words of a component: JSX text and string literals that read as prose. */
export function visibleText(source: string): string[] {
  const out: string[] = [];
  const code = source.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");
  for (const m of code.matchAll(/>([^<>{}]*[A-Za-z][^<>{}]*)</g)) {
    const text = m[1].trim();
    // code between two tags (") : !rows ? (") is not text
    if (text && !/[=;]|=>|\b(const|return|function)\b|^[)\s:]|\?\s*\(|&&|\|\||!\w/.test(text)) out.push(text);
  }
  for (const m of code.matchAll(/(["'`])((?:(?!\1)[^\\\n]|\\.)*)\1/g)) {
    const text = m[2];
    // prose: starts with a capital, has a space, and is not a class list or a path
    if (/^[A-Z][a-z]/.test(text) && / /.test(text) && !/^[\w-]+(?: [\w:[\]/%.()-]+)+$/.test(text.replace(/[,.?:;'"]/g, "")) === false) out.push(text);
    else if (/^[A-Z][a-z]+[^/]* [a-z]/.test(text) && !/[{}<>=]|\$\{/.test(text)) out.push(text);
  }
  return out;
}

const ui = (r: Rule) => r.applies.includes("ui");

export function findings(text: string): string[] {
  const found: string[] = [];
  for (const r of rules.banned_phrases.filter(ui)) if (new RegExp(`(?<!\\w)(?:${r.pattern})(?!\\w)`, "i").test(text)) found.push(`${r.reason}: ${r.pattern}`);
  for (const r of rules.banned_chars.filter(ui)) if (text.includes(r.char as string)) found.push(r.reason);
  for (const r of rules.terms.filter(ui)) if (new RegExp(`\\b(?:${r.pattern})\\b`, "i").test(text)) found.push(`say ${r.use}`);
  if (/\b\w+\((?:s|es)\)/.test(text)) found.push("computed plural");
  return found;
}

describe("app copy", () => {
  it("the rules catch what they should and pass plain words", () => {
    expect(findings("No approvals waiting.")).toEqual([]);
    expect(findings("Something went wrong")).not.toEqual([]);
    expect(findings("Vendor terms")).toEqual(["say supplier"]);
    expect(findings("3 order(s)")).toEqual(["computed plural"]);
    expect(findings("Done ✓")).toEqual(["check mark"]);
  });

  it("every component's visible text passes the ui rules", () => {
    const bad: string[] = [];
    for (const file of files(SRC)) {
      for (const text of visibleText(readFileSync(file, "utf8"))) {
        const f = findings(text);
        if (f.length) bad.push(`${relative(SRC, file)}: "${text.slice(0, 80)}" (${f.join("; ")})`);
      }
    }
    expect(bad).toEqual([]);
  });
});
