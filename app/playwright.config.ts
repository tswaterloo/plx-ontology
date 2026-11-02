import { defineConfig } from "@playwright/test";

// Walkthrough of the demo flow against operator_api's fixture mode
// (`python3 -m operator_api serve --mock`). PLX_API_PYTHON picks a
// Python that has fastapi + uvicorn. Browsers: PLAYWRIGHT_BROWSERS_PATH
// (e.g. /data/tools/pw-browsers), or a system Chromium via PW_CHROMIUM.
const APP_PORT = Number(process.env.E2E_APP_PORT || 3210);
const MOCK_PORT = Number(process.env.E2E_MOCK_PORT || 8797);
const executablePath = process.env.PW_CHROMIUM || undefined;
const python = process.env.PLX_API_PYTHON || "python3";
export const MOCK_ORIGIN = `http://127.0.0.1:${MOCK_PORT}`;
// E2E_BASE_URL: run against an app that is already up (the live app, e.g.
// http://127.0.0.1:3000 with its API on :8780); nothing is started or stopped then.
const external = process.env.E2E_BASE_URL?.replace(/\/+$/, "") || null;

export default defineConfig({
  testDir: "./e2e",
  timeout: 60_000,
  expect: { timeout: 10_000 },
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [["list"]],
  use: {
    baseURL: external ?? `http://127.0.0.1:${APP_PORT}`,
    launchOptions: executablePath ? { executablePath } : {},
    trace: "retain-on-failure",
  },
  projects: [
    { name: "desktop", use: { viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1 } },
    { name: "phone", use: { viewport: { width: 390, height: 844 }, deviceScaleFactor: 2, isMobile: true, hasTouch: true } },
  ],
  webServer: external ? [] : [
    {
      // Fixture data only; no business system, no credentials.
      command:
        `cd .. && env -u PLX_WORKBENCH_API_KEY -u OPENAI_API_KEY ${python} -m operator_api serve --mock ` +
        // No --allowed-origin: the app's own origin reaches the API through next start's
        // /api rewrite (X-Forwarded-Host), as in the deployed topology.
        // E2E_MOCK_TICKET: sign-in takes that access ticket (e2e/access-link.spec.ts).
        `--port ${MOCK_PORT} --insecure-cookie --mock-step-delay 0.3` +
        (process.env.E2E_MOCK_TICKET ? " --mock-ticket-env E2E_MOCK_TICKET" : ""),
      url: `${MOCK_ORIGIN}/api/health`,
      reuseExistingServer: false,
    },
    {
      // Production build, so the walkthrough exercises what Caddy would serve.
      command: `npx next build && npx next start -p ${APP_PORT} -H 127.0.0.1`,
      url: `http://127.0.0.1:${APP_PORT}/signin`,
      reuseExistingServer: false,
      timeout: 240_000,
      env: { PLX_API_ORIGIN: `http://127.0.0.1:${MOCK_PORT}`, NEXT_TELEMETRY_DISABLED: "1" },
    },
  ],
});
