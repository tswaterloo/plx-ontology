import type { NextConfig } from "next";

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
