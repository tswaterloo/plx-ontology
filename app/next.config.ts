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
  poweredByHeader: false,
  compress: false, // never buffer the answer stream; Caddy compresses static assets
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${apiOrigin}/api/:path*` }];
  },
  async headers() {
    return [{ source: "/:path*", headers: securityHeaders }];
  },
};

export default config;
