import { expect, test, type Locator, type Page } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";

// The CEO demo path, end to end:
//   Today and the briefing -> the late list -> one late PO (ETA confirmed?, how we know,
//   the follow-up draft) -> the customer order it puts at risk (late materials) -> a sales
//   order's margin and why -> the customer's profitability -> Ask, four questions (fast
//   path, agent path, profitability, what changed).
// Every step takes a screenshot and fails on an error state, an empty card, a loading
// skeleton left behind or a raw float on the page.
//
// Mock (default): `npx playwright test e2e/demo-walk.spec.ts --project=desktop` starts
// operator_api --mock and a production build (playwright.config.ts). Fixture data only.
//
// Live: point it at a running app; nothing is started, restarted or written.
//   E2E_BASE_URL=http://127.0.0.1:3000
//   E2E_SIGNIN=ticket  E2E_TICKET=<from deploy/demo-access-link.sh>   (never echoed)
//     or E2E_SIGNIN=storage  E2E_STORAGE_STATE=<a saved Playwright storage state file>
//   E2E_PO=106093  E2E_SO="SO 1234"  E2E_CUSTOMER=DIVE11  (the records the script names)
//   E2E_MARGIN_PATH=/object/SalesOrder/<id>  (a sales order with a margin; default: E2E_SO's page)
//   E2E_Q_FAST / E2E_Q_AGENT / E2E_Q_PROFIT / E2E_Q_CHANGED  (the four questions)
//   E2E_SHOTS=<dir>  (default e2e/screenshots/demo-walk/<mock|live>)

const env = process.env;
const live = Boolean(env.E2E_BASE_URL);
const target = live ? "live" : "mock";
const shots = env.E2E_SHOTS || path.join(__dirname, "screenshots", "demo-walk", target);

const PO = env.E2E_PO || "90412";
const SO = env.E2E_SO || "SO 77120";
const CUSTOMER = env.E2E_CUSTOMER || "glow";
const MARGIN_PATH = env.E2E_MARGIN_PATH || "";
const QUESTIONS = {
  fast: env.E2E_Q_FAST || "Which purchase orders are late?",
  agent: env.E2E_Q_AGENT || "why is the soap order from that canadian guy late",
  profit: env.E2E_Q_PROFIT || "is Glow Botanicals profitable, and why?",
  changed: env.E2E_Q_CHANGED || "What changed since my last visit?",
};
// Live agent answers took 30-110 s on 31 Oct; the fast path answers in a few seconds.
const ASK_TIMEOUT = Number(env.E2E_ASK_TIMEOUT_MS || (live ? 180_000 : 20_000));
const FAST_BUDGET = Number(env.E2E_FAST_BUDGET_MS || 15_000);
const PAGE_TIMEOUT = Number(env.E2E_PAGE_TIMEOUT_MS || (live ? 90_000 : 15_000));

const escape = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
/** "106093", "PO 106093" and "PO106093" all name the same link. */
// Cell text runs together ("PO 90412Maple Leaf"), so the number ends at a non-digit, not \b.
const poName = new RegExp(`(PO\\s*)?(?<!\\d)${escape(PO.replace(/^PO\s*/i, ""))}(?!\\d)`, "i");
const soName = new RegExp(`(?<![A-Za-z0-9])${escape(SO).replace(/ /g, "\\s*")}(?!\\d)`, "i");

/** Raw number artefacts: float noise, exponent notation, NaN and friends. */
const RAW_NUMBER = [
  /\d\.\d{6,}/, // 0.30000000000000004, 1.3636363636
  /\d\.\d{3,}\s?%/, // 17.8045%
  /\b\d+(\.\d+)?e[-+]?\d+\b/i, // 1e-7, 4.41e+06
  /\bNaN\b|\bInfinity\b|\bundefined\b|\[object Object\]/,
];

let step = 0;
async function shot(page: Page, name: string) {
  step += 1;
  await page.waitForTimeout(500); // pages fade in
  await page.screenshot({ path: path.join(shots, `${String(step).padStart(2, "0")}-${name}.png`), fullPage: true });
}

/** No error state, no empty card, no skeleton, no raw float on what the page shows. */
async function expectClean(page: Page, where: string) {
  await expect(page.getByTestId("checking"), `${where}: sections still loading`).toHaveCount(0, { timeout: PAGE_TIMEOUT });
  await expect(page.locator('[data-testid^="section-loading-"]'), `${where}: skeleton left`).toHaveCount(0);
  // Next.js's route announcer is an alert that reads the page title: not an error.
  await expect(page.locator('[role="alert"]:not(#__next-route-announcer__)'), `${where}: error shown`).toHaveCount(0);
  await expect(page.getByTestId("checks-failed"), `${where}: a section failed`).toHaveCount(0);
  await expect(page.getByTestId("server-notice"), `${where}: server notice`).toHaveCount(0);
  await expect(page.getByTestId("empty-state"), `${where}: empty state`).toHaveCount(0);
  const text = await page.locator("main").innerText();
  expect(text, `${where}: raw JSON`).not.toMatch(/\{\s*"(code|error|detail)"\s*:/);
  expect(text, `${where}: stack trace`).not.toMatch(/\bat \S+ \(.*:\d+:\d+\)|Unhandled Runtime Error|TypeError:|Traceback/);
  for (const re of RAW_NUMBER) expect(text, `${where}: raw number ${re}`).not.toMatch(re);
  // A card (the Card surface) whose only text is its title, or none at all.
  const empty = await page.evaluate(() =>
    [...document.querySelectorAll("main [data-card]")]
      .filter((c) => (c as HTMLElement).offsetParent !== null)
      .filter((c) => {
        const title = c.querySelector("h2")?.textContent?.trim() ?? "";
        const all = (c as HTMLElement).innerText.trim();
        return all.replace(title, "").trim().length === 0;
      })
      .map((c) => c.querySelector("h2")?.textContent?.trim() || (c as HTMLElement).dataset.testid || "untitled card"),
  );
  expect(empty, `${where}: empty cards`).toEqual([]);
}

async function signIn(page: Page) {
  const how = env.E2E_SIGNIN || (live ? "ticket" : "demo");
  if (how === "storage") {
    await page.goto("/");
  } else if (how === "ticket") {
    const ticket = env.E2E_TICKET;
    if (!ticket) throw new Error("E2E_SIGNIN=ticket needs E2E_TICKET (an access-link ticket)");
    // The fragment never reaches a server, and the app strips it at once.
    await page.goto(`/#ticket=${encodeURIComponent(ticket)}`);
  } else {
    await page.goto("/signin");
    await page.getByRole("button", { name: "Continue with demo sign-in" }).click();
  }
  await expect(page).not.toHaveURL(/\/signin/, { timeout: PAGE_TIMEOUT });
  expect(page.url()).not.toContain("ticket");
}

/** Asks from Today's search-or-ask bar (a fresh conversation each time); the finished card and its time. */
async function ask(page: Page, question: string): Promise<{ card: Locator; ms: number }> {
  await page.goto("/");
  await page.keyboard.press("Control+k");
  const box = page.getByTestId("palette-input");
  await box.fill(question);
  const started = Date.now();
  await box.press("Enter");
  await expect(page).toHaveURL(/\/ask\?q=/);
  const card = page.getByTestId("answer-card").last();
  await expect(card).toHaveAttribute("aria-busy", "false", { timeout: ASK_TIMEOUT });
  const ms = Date.now() - started;
  await expect(card.getByTestId("direct-answer")).toHaveText(/\S.{20,}/s);
  // Partly answered is honest and fine; "not on record", a refusal or no assistant is not a demo answer.
  const badge = card.getByTestId("outcome-badge");
  if (await badge.count()) await expect(badge).not.toHaveText(/Not on record|Can.t answer|Assistant unavailable/);
  return { card, ms };
}

test.use(env.E2E_SIGNIN === "storage" && env.E2E_STORAGE_STATE ? { storageState: env.E2E_STORAGE_STATE } : {});

test("demo walk: briefing, late PO, order at risk, margin, Ask", async ({ page }, info) => {
  test.skip(info.project.name !== "desktop", "the demo is shown on a desktop screen");
  test.setTimeout(live ? 20 * 60_000 : 240_000);
  fs.mkdirSync(shots, { recursive: true });
  step = 0;
  await signIn(page);

  // 1. Today: the headline, the numbers and what needs you, ranked.
  await page.goto("/");
  await expect(page.getByTestId("headline")).toBeVisible({ timeout: PAGE_TIMEOUT });
  await expect(page.getByTestId("stat-late")).toBeVisible();
  await expect(page.getByTestId("needs-you").getByTestId("briefing-item").first()).toBeVisible({ timeout: PAGE_TIMEOUT });
  await expectClean(page, "today");
  // one data cut-off on every screen: Today's "Data as of" day is the late list's
  const day = /\d{1,2} [A-Z][a-z]{2}/;
  const asOf = ((await page.getByTestId("home-freshness").textContent()) ?? "").match(day)?.[0];
  expect(asOf, "Today states no data date").toBeTruthy();
  await shot(page, "today");

  // 2. The proactive briefing, in full.
  await page.getByTestId("see-all").click();
  await expect(page).toHaveURL(/\/briefing/);
  await expect(page.locator('[data-testid="briefing-item"], [data-testid="briefing-row"]').first()).toBeVisible({ timeout: PAGE_TIMEOUT });
  await expectClean(page, "briefing");
  await shot(page, "briefing");

  // 3. Late orders, from the Today tile.
  await page.goto("/");
  await page.getByTestId("stat-late").click();
  await expect(page).toHaveURL(/\/late/);
  const rows = page.getByTestId("late-row");
  await expect(rows.first()).toBeVisible({ timeout: PAGE_TIMEOUT });
  const row = rows.filter({ hasText: poName }).first();
  await expect(row, `PO ${PO} is not on the late list`).toBeVisible();
  await expect(page.getByTestId("late-eta").first()).toHaveText(/\S/);
  await expect(page.getByTestId("late-all-count"), "the late list states another data date than Today").toContainText(`as of ${asOf}`);
  await expectClean(page, "late list");
  await shot(page, "late-list");

  // 4. One late PO: ETA confirmed?, how we know, who to chase and the follow-up draft.
  // the row opens the PO beside the list; its page is one step on
  await row.click();
  await page.getByTestId("open-record").click();
  await expect(page).toHaveURL(/\/(po|object\/PurchaseOrder)\//);
  await expect(page.getByTestId("object-title")).toContainText(poName, { timeout: PAGE_TIMEOUT });
  await expectClean(page, "PO page");
  await expect(page.getByTestId("eta-card").getByTestId("eta-badge")).toHaveText(/\S/);
  const trail = page.getByTestId("evidence-trail");
  await expect(trail).toBeVisible();
  expect(await trail.getByTestId("trail-step").count(), "the PO's trail has no steps").toBeGreaterThan(0);
  const drafts = page.getByTestId("proposal");
  expect(await drafts.count(), "no follow-up draft or proposed update on the PO").toBeGreaterThan(0);
  await expect(drafts.first()).toContainText(/Draft|Proposed update/);
  await shot(page, "po");

  // 5. The customer order it puts at risk, with its late materials.
  const soLink = page.locator("main").getByRole("link", { name: soName }).first();
  await expect(soLink, `no link from PO ${PO} to ${SO}`).toBeVisible();
  await soLink.click();
  await expect(page.getByTestId("object-title")).toContainText(soName, { timeout: PAGE_TIMEOUT });
  await expectClean(page, "order at risk");
  const materials = page.getByTestId("materials");
  await expect(materials, `${SO} shows no materials walk`).toBeVisible();
  await expect(materials.getByRole("link", { name: poName })).toBeVisible();
  await shot(page, "order-at-risk");

  // 6. A sales order's margin and why.
  if (MARGIN_PATH) await page.goto(MARGIN_PATH);
  const margin = page.getByTestId("margin");
  await expect(margin).toBeVisible({ timeout: PAGE_TIMEOUT });
  await expectClean(page, "order margin");
  await expect(margin.getByTestId("margin-verdict").first()).toHaveText(/\S/);
  await expect(margin.getByTestId("margin-why")).toHaveText(/\S.{20,}/s);
  await expect(margin.getByTestId("margin-sources")).toBeVisible();
  await shot(page, "order-margin");

  // 7. The customer's profitability.
  await page.goto(`/customer/${encodeURIComponent(CUSTOMER)}`);
  await expect(page.getByTestId("object-title")).toBeVisible({ timeout: PAGE_TIMEOUT });
  await expect(page.getByTestId("friendly-chooser"), `customer ${CUSTOMER} is not one record`).toHaveCount(0);
  await expectClean(page, "customer");
  await expect(page.getByTestId("margin")).toBeVisible();
  await shot(page, "customer");

  // 8. Ask: fast path, agent path, profitability, what changed.
  const fast = await ask(page, QUESTIONS.fast);
  expect(fast.ms, `the fast-path answer took ${fast.ms} ms`).toBeLessThanOrEqual(FAST_BUDGET);
  await expectClean(page, "ask fast");
  await shot(page, "ask-fast");

  const agent = await ask(page, QUESTIONS.agent);
  expect(await agent.card.getByTestId("source-chip").count(), "the agent answer cites nothing").toBeGreaterThan(0);
  await expectClean(page, "ask agent");
  await shot(page, "ask-agent");

  const profit = await ask(page, QUESTIONS.profit);
  await expect(profit.card.getByTestId("profitability")).toBeVisible();
  await expectClean(page, "ask profitability");
  await shot(page, "ask-profit");

  const changed = await ask(page, QUESTIONS.changed);
  expect(await changed.card.getByTestId("source-chip").count(), "what-changed cites nothing").toBeGreaterThan(0);
  await expectClean(page, "ask what changed");
  await shot(page, "ask-changed");

  info.annotations.push({ type: "timings", description: `fast ${fast.ms} ms, agent ${agent.ms} ms, profit ${profit.ms} ms, changed ${changed.ms} ms` });
  console.log(`demo-walk ${target}: fast ${fast.ms} ms, agent ${agent.ms} ms, profit ${profit.ms} ms, changed ${changed.ms} ms; screenshots in ${shots}`);
});
