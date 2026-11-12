import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const root = join(__dirname, "..", "..");
const pkg = JSON.parse(readFileSync(join(root, "package.json"), "utf8"));
const lock = JSON.parse(readFileSync(join(root, "package-lock.json"), "utf8")) as { packages: Record<string, { engines?: { node?: string }; dev?: boolean; optional?: boolean }> };

type V = [number, number, number];
const parse = (s: string): V => {
  const [a = "0", b = "0", c = "0"] = s.split(".");
  return [Number(a), Number(b), Number(c)];
};
const ge = (a: V, b: V) => a[0] - b[0] || a[1] - b[1] || a[2] - b[2];
/** Does `v` meet one alternative: `^x.y.z` (same major, at least), `>= x[.y.z]` or a bare major `N`? */
function meets(v: V, alt: string): boolean {
  const caret = alt.match(/^\^v?(\d+(?:\.\d+){0,2})$/);
  if (caret) return v[0] === parse(caret[1])[0] && ge(v, parse(caret[1])) >= 0;
  const gte = alt.match(/^>=\s*v?(\d+(?:\.\d+){0,2})$/);
  if (gte) return ge(v, parse(gte[1])) >= 0;
  const major = alt.match(/^(\d+)$/);
  if (major) return v[0] === Number(major[1]);
  throw new Error(`engine range not understood: ${alt}`);
}
const alts = (range: string) => range.split("||").map((x) => x.trim());

describe("the declared Node range runs the whole app (WEB-026) and no script is a removed command (WEB-025)", () => {
  it("every locked dependency accepts the lowest Node of each range we declare", () => {
    const ours = alts(pkg.engines.node);
    expect(ours.length).toBeGreaterThan(0);
    const lowest = ours.map((a) => parse(a.replace(/^(\^|>=)\s*/, "")));
    for (const [name, info] of Object.entries(lock.packages)) {
      const need = info.engines?.node;
      // a package that only some platforms install cannot block the others
      if (!name || !need || info.optional) continue;
      for (const v of lowest) expect(alts(need).some((a) => meets(v, a)), `${name} (${need}) vs Node ${v.join(".")}`).toBe(true);
    }
  });

  it("package scripts name no command the installed Next no longer has", () => {
    expect(Object.values(pkg.scripts as Record<string, string>).join("\n")).not.toMatch(/next lint/);
  });
});
