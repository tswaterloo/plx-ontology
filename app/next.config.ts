import { createHash } from "node:crypto";
import { readdirSync, readFileSync } from "node:fs";
import path from "node:path";
import type { NextConfig } from "next";

// This build's identity for the desktop page store (src/lib/read-store.ts RELEASE): a digest of
// the page's own source, so an answer kept by another build is read again, never drawn. It is
// the same on every load of this config for the same source (no clock, no git).
function sourceDigest(dir: string): string {
  const h = createHash("sha256");
  const walk = (d: string) => {
    for (const e of readdirSync(d, { withFileTypes: true }).sort((a, b) => a.name.localeCompare(b.name))) {
      const p = path.join(d, e.name);
      if (e.isDirectory()) walk(p);
      else if (/\.(tsx?|css|json)$/.test(e.name)) h.update(path.relative(dir, p)).update(readFileSync(p));
    }
  };
  walk(dir);
  return h.digest("hex").slice(0, 16);
}

// In production Caddy sends /api/* straight to operator_api, so this rewrite only
// serves local development and the Playwright walkthrough (operator_api on :8780 by default).
const apiOrigin = process.env.PLX_API_ORIGIN || "http://127.0.0.1:8780";

const securityHeaders = [
  { key: "X-Content-Type-Options", value: "nosniff" },
  { key: "Referrer-Policy", value: "same-origin" },
  { key: "X-Frame-Options", value: "DENY" },
  { key: "Permissions-Policy", value: "camera=(), microphone=(), geolocation=()" },
];

const config: NextConfig = {
  env: { NEXT_PUBLIC_PLX_RELEASE: sourceDigest(path.join(process.cwd(), "src")) },
  reactStrictMode: true,
  devIndicators: false, // dev only: no floating badge over the page
  poweredByHeader: false,
  compress: false, // never buffer the answer stream; Caddy compresses static assets
  // the dev rewrite's proxy gives up at 30 s by default; a record page takes up to ~2 min
  // to gather (production goes through Caddy, not this rewrite)
  experimental: { proxyTimeout: 300_000 },
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${apiOrigin}/api/:path*` }];
  },
  // the old pages, folded into Home (its Alerts and Late POs tabs), Meetings, Team, Work log
  // and the record page; temporary so the routes stay free to change. The ?query is carried
  // over (/late?supplier=X opens Home's Late POs tab on that supplier) and the browser keeps
  // a #fragment (/watching#data-health).
  async redirects() {
    return [
      // Records became Orders, Customers, Suppliers, Products and More; the old address keeps working
      { source: "/records", has: [{ type: "query", key: "kind", value: "customers" }], destination: "/customers", permanent: false },
      { source: "/records", has: [{ type: "query", key: "kind", value: "suppliers" }], destination: "/suppliers", permanent: false },
      { source: "/records", has: [{ type: "query", key: "kind", value: "products" }], destination: "/products", permanent: false },
      { source: "/records", has: [{ type: "query", key: "kind", value: "(documents|production|lists|all)" }], destination: "/more", permanent: false },
      { source: "/records", destination: "/orders", permanent: false },
      { source: "/autopilot", has: [{ type: "query", key: "item" }], destination: "/assist", permanent: false },
      { source: "/autopilot", destination: "/assist?show=switches", permanent: false },
      { source: "/watching", destination: "/?show=alerts", permanent: false },
      { source: "/late", destination: "/?show=late", permanent: false },
      { source: "/my-work", destination: "/", permanent: false },
      { source: "/briefing", destination: "/?show=alerts#alerts", permanent: false },
      { source: "/prepare", destination: "/meetings", permanent: false },
      { source: "/weekly", destination: "/meetings?view=weekly", permanent: false },
      { source: "/brief/:type/:id*", destination: "/meetings/brief/:type/:id*", permanent: false },
      { source: "/approvals", destination: "/team#approvals", permanent: false },
      { source: "/rules", destination: "/?show=alerts", permanent: false },
      { source: "/actions", destination: "/activity", permanent: false },
      { source: "/replies", destination: "/?view=reply", permanent: false },
      { source: "/workload", destination: "/team#workload", permanent: false },
      { source: "/labour", destination: "/team#labour", permanent: false },
      { source: "/meetings/action-items", destination: "/meetings?view=follow-ups", permanent: false },
      { source: "/pack/:type/:id*", destination: "/object/:type/:id*#documents", permanent: false },
      // the record page's first address (before 10-29), still in older mail and notes
      { source: "/o/:type/:id*", destination: "/object/:type/:id*", permanent: false },
    ];
  },
  async headers() {
    return [{ source: "/:path*", headers: securityHeaders }];
  },
};

export default config;
