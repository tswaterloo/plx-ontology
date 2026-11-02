import { expect, test } from "@playwright/test";

// The access link, as the demo opens it: https://<host>/#ticket=<ticket> in a fresh
// browser signs in on the first try, then Today, a purchase order and Ask all work from
// the app (the API's POST routes accept the app's own origin through the proxy in front).
//
// Mock: `E2E_MOCK_TICKET=<any 16+ characters> npx playwright test e2e/access-link.spec.ts`
// starts operator_api --mock taking that ticket (playwright.config.ts) and a production
// build; without E2E_MOCK_TICKET the mock takes no ticket and this spec is skipped.
// Live: E2E_BASE_URL=http://127.0.0.1:3000 E2E_TICKET=<from deploy/demo-access-link.sh>
// (never echoed); the same ticket signs in desktop and phone (E2E_TICKET_PHONE overrides).

const env = process.env;
const live = Boolean(env.E2E_BASE_URL);
const PAGE_TIMEOUT = live ? 120_000 : 15_000;
const ASK_TIMEOUT = Number(env.E2E_ASK_TIMEOUT_MS || (live ? 240_000 : 20_000));
const QUESTION = env.E2E_Q_FAST || "Which purchase orders are late?";

function ticketFor(project: string): string | undefined {
  if (!live) return env.E2E_MOCK_TICKET;
  return project === "phone" ? env.E2E_TICKET_PHONE || env.E2E_TICKET : env.E2E_TICKET;
}

test("access link signs in on the first try; Today, a PO and Ask work", async ({ page }, info) => {
  test.setTimeout(live ? 10 * 60_000 : 120_000);
  const ticket = ticketFor(info.project.name);
  test.skip(!ticket, `no ticket for the ${info.project.name} project`);

  // 1. The link: signed in without touching the sign-in form, the ticket gone from the URL.
  await page.goto(`/#ticket=${encodeURIComponent(ticket!)}`);
  await expect(page.getByTestId("headline")).toBeVisible({ timeout: PAGE_TIMEOUT });
  await expect(page).not.toHaveURL(/\/signin/);
  expect(page.url()).not.toContain("ticket");

  // From here on, signed in: no API call may be refused.
  const refused: string[] = [];
  page.on("response", (r) => {
    if (r.url().includes("/api/") && (r.status() === 401 || r.status() === 403)) {
      refused.push(`${r.status()} ${new URL(r.url()).pathname}`);
    }
  });

  // 2. The session holds across a full reload (the cookie was kept).
  await page.reload();
  await expect(page.getByTestId("headline")).toBeVisible({ timeout: PAGE_TIMEOUT });
  await expect(page).not.toHaveURL(/\/signin/);

  // 3. A purchase order page (E2E_PO_PATH live, e.g. /po/<number>; the fixture PO in mock).
  await page.goto(env.E2E_PO_PATH || "/po/90412");
  await expect(page.getByTestId("object-title")).toBeVisible({ timeout: PAGE_TIMEOUT });

  // 4. Ask, from the app: a POST through the same origin, answered.
  await page.goto("/");
  const box = page.getByRole("combobox", { name: "Ask a question" });
  await box.fill(QUESTION);
  await box.press("Enter");
  await expect(page).toHaveURL(/\/ask\?q=/);
  const card = page.getByTestId("answer-card").last();
  await expect(card).toHaveAttribute("aria-busy", "false", { timeout: ASK_TIMEOUT });
  await expect(card.getByTestId("direct-answer")).toHaveText(/\S.{20,}/s);
  const badge = card.getByTestId("outcome-badge");
  if (await badge.count()) await expect(badge).not.toHaveText(/Can.t answer|Assistant unavailable/);

  expect(refused, "an API call was refused").toEqual([]);
});

test("a wrong access link lands on sign-in with a plain message", async ({ page }, info) => {
  test.skip(!ticketFor(info.project.name) || info.project.name !== "desktop", "needs a ticket-taking server");
  test.setTimeout(live ? 4 * 60_000 : 60_000);
  await page.goto(`/#ticket=${encodeURIComponent("not-a-valid-access-ticket")}`);
  await expect(page).toHaveURL(/\/signin/, { timeout: PAGE_TIMEOUT });
  expect(page.url()).not.toContain("ticket=");
  await expect(page.getByRole("alert").filter({ hasText: "access link has expired or isn't valid" })).toBeVisible({
    timeout: PAGE_TIMEOUT,
  });
});
