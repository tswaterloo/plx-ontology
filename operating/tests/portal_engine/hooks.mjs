// Module hooks for running Portal TypeScript stand-alone (no node_modules, no network).
// "@/..." resolves into PORTAL_SRC; the Prisma client module is replaced by fake_db.mjs; every
// bare package import (zod, prisma, pg, next, ...) becomes an inert stub; JSON imports load.
import { readFileSync, statSync } from "node:fs";
import { pathToFileURL, fileURLToPath } from "node:url";
import path from "node:path";

const SRC = process.env.PORTAL_SRC;
const FAKE_DB = pathToFileURL(path.join(path.dirname(fileURLToPath(import.meta.url)), "fake_db.mjs")).href;
const isFile = (p) => { try { return statSync(p).isFile(); } catch { return false; } };
const BARE_RE = /^(?!node:)(?![.@]\/)[^./][^"']*$|^@(?!\/)[^"']+$/;
const isBare = (s) => BARE_RE.test(s) || s.startsWith("@/generated/");

export async function resolve(spec, ctx, next) {
  if (spec === "@/lib/db/db" || spec === "@/lib/db") return { url: FAKE_DB, shortCircuit: true };
  let p = null;
  if (spec.startsWith("@/")) p = path.join(SRC, spec.slice(2));
  else if ((spec.startsWith("./") || spec.startsWith("../")) && ctx.parentURL?.startsWith("file:")) {
    p = path.resolve(path.dirname(fileURLToPath(ctx.parentURL)), spec);
  }
  if (p) {
    for (const c of [p, p + ".ts", p + ".tsx", path.join(p, "index.ts")]) {
      if (isFile(c)) return { url: pathToFileURL(c).href, shortCircuit: true };
    }
    throw new Error("unresolved " + spec);
  }
  return next(spec, ctx);
}

// Rewrite `import ... from "<bare package>"` into reads of a stub; type-only imports vanish.
function rewrite(src) {
  return src
    .replace(/import\s+(type\s+)?([\s\S]*?)\s+from\s+["']([^"']+)["'];?/g, (m, typeOnly, clause, spec) => {
      if (!isBare(spec)) return m;
      if (typeOnly) return "";
      const lit = JSON.stringify(spec);
      const rest = clause.trim();
      const ns = rest.match(/^\*\s+as\s+(\w+)$/);
      if (ns) return `const ${ns[1]} = globalThis.__stub(${lit});`;
      const out = [];
      const brace = rest.match(/\{([\s\S]*)\}/);
      const def = rest.replace(/\{[\s\S]*\}/, "").replace(/,/g, "").trim();
      if (def) out.push(`const ${def} = globalThis.__stub(${lit});`);
      if (brace) {
        const names = brace[1].split(",").map((s) => s.trim()).filter((s) => s && !s.startsWith("type "));
        if (names.length) out.push(`const { ${names.map((n) => n.replace(/\s+as\s+/, ": ")).join(", ")} } = globalThis.__stub(${lit});`);
      }
      return out.join(" ");
    })
    .replace(/^import\s+["']([^"']+)["'];?$/gm, (m, spec) => (isBare(spec) ? "" : m));
}

export async function load(url, ctx, next) {
  if (url.startsWith("file:") && /\.tsx?$/.test(url) && fileURLToPath(url).startsWith(SRC)) {
    return { format: "module-typescript", source: rewrite(readFileSync(fileURLToPath(url), "utf8")), shortCircuit: true };
  }
  if (url.startsWith("file:") && url.endsWith(".json")) {
    return { format: "module", source: "export default " + readFileSync(fileURLToPath(url), "utf8") + ";", shortCircuit: true };
  }
  return next(url, ctx);
}
