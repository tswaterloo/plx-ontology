#!/usr/bin/env node
// Per-route first-load JS of the last `next build` (Turbopack prints no sizes).
// For each app route: the root main files plus every chunk its client-reference manifest
// lists for the route's layouts and page, deduplicated; raw and gzip bytes.
// Usage: node scripts/route-js-size.mjs [--json]
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, relative } from "node:path";
import { gzipSync } from "node:zlib";

const next = join(process.cwd(), ".next");
const build = JSON.parse(readFileSync(join(next, "build-manifest.json"), "utf8"));
// polyfillFiles are nomodule-only: modern browsers never load them, so they are not counted
const root = [...build.rootMainFiles];

const sizes = new Map();
function size(file) {
  if (!sizes.has(file)) {
    const buf = readFileSync(join(next, file));
    sizes.set(file, { raw: buf.length, gz: gzipSync(buf, { level: 9 }).length });
  }
  return sizes.get(file);
}

function manifests(dir, out = []) {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) manifests(p, out);
    else if (name === "page_client-reference-manifest.js") out.push(p);
  }
  return out;
}

const rows = [];
for (const file of manifests(join(next, "server", "app"))) {
  globalThis.__RSC_MANIFEST = {};
  new Function(readFileSync(file, "utf8"))();
  const [key, m] = Object.entries(globalThis.__RSC_MANIFEST)[0];
  const route = "/" + relative(join(next, "server", "app"), file).split("/").slice(0, -1)
    .filter((s) => !s.startsWith("(")).join("/");
  // the route's own entries: its layouts and its page (not sibling error/not-found)
  const segs = key.replace(/\/page$/, "");
  const files = new Set(root);
  for (const [entry, chunks] of Object.entries(m.entryJSFiles)) {
    const e = entry.replace("[project]/src/app", "");
    const isLayout = e.endsWith("/layout") && (segs + "/").startsWith(e.slice(0, -"layout".length));
    if (isLayout || e === key) for (const c of chunks) files.add(c);
  }
  let raw = 0, gz = 0;
  for (const f of files) { const s = size(f); raw += s.raw; gz += s.gz; }
  rows.push({ route: route === "/" ? "/" : route.replace(/\/$/, ""), raw, gz, chunks: files.size });
}
rows.sort((a, b) => a.route.localeCompare(b.route));
if (process.argv.includes("--json")) {
  console.log(JSON.stringify(rows, null, 1));
} else {
  const kb = (n) => (n / 1024).toFixed(1).padStart(7);
  console.log("route".padEnd(34) + "   raw KB    gz KB  chunks");
  for (const r of rows) console.log(r.route.padEnd(34) + kb(r.raw) + "  " + kb(r.gz) + "  " + String(r.chunks).padStart(5));
}
