import { expect, test, type Page } from "@playwright/test";
import path from "node:path";
import { exercise, focusFaults, layoutFaults, rawErrors, settle, signIn, watch } from "./helpers";

// Usability pass in a real browser over operator_api --mock (fixture data): every page at
// 1280 and 1440 (desktop project) and 390 (phone project), light and dark. On each page it
// opens and selects every control it can reach, then checks: no console or page errors,
// nothing scrolls sideways or sits off screen, no select cuts its own chosen text, keyboard
// focus is visible, and loading, empty and error states are worded and aligned with the page.
// Send, confirm, approve, execute, undo and switch endpoints are stubbed and must never be hit.

const shots = process.env.USABILITY_SHOTS || path.join(__dirname, "screenshots", "usability");

// a control that went away fails its read in seconds instead of waiting for the test timeout
// (10 s: at load ~120 a starved page takes several seconds to answer a ready control)
test.use({ actionTimeout: 10_000 });

const ALL_PAGES = [
  "/",
  "/my-work",
  "/late",
  "/autopilot",
  "/meetings",
  "/watching",
  "/activity",
  "/team",
  "/ops",
  "/rd-projects",
  "/feedback",
  "/ask",
  "/object/PurchaseOrder/PurchaseOrder%3Ademo%3A90412",
  "/source?handle=bc%3A%2F%2Fdemo%2FpurchaseOrders%2F90412",
  // the friendly record paths, the work log, trust and a prep brief (every app page but
  // sign-in, which runs before, and a meeting's follow-up, which the mock keeps none of)
  "/po/90412",
  "/supplier/Ridgeway%20Labels",
  "/customer/Glow%20Botanicals",
  "/ao/AO-7712",
  "/work-log",
  "/trust",
  "/meetings/brief/Supplier/Supplier%3Ademo%3Amaple-leaf",
];
// USABILITY_PAGES (comma-separated paths): walk only those, to look again at one page
const PAGES = process.env.USABILITY_PAGES ? ALL_PAGES.filter((p) => process.env.USABILITY_PAGES!.split(",").includes(p)) : ALL_PAGES;


async function shot(page: Page, name: string) {
  await page.screenshot({ path: path.join(shots, `${name}.png`), fullPage: true, timeout: 20_000 });
}

const WIDTHS: Record<string, number[]> = { desktop: [1440, 1280], phone: [390] };

for (const theme of ["light", "dark"] as const) {
  test(`every page and control works (${theme})`, async ({ page }, info) => {
    // 21 pages, every control on each: a loaded machine needs more than ten minutes
    test.setTimeout(1_500_000);
    const w = await watch(page);
    await page.emulateMedia({ colorScheme: theme });
    await signIn(page);
    const faults: string[] = [];
    for (const width of WIDTHS[info.project.name] ?? [1440]) {
      if (info.project.name === "desktop") await page.setViewportSize({ width, height: 900 });
      for (const route of PAGES) {
        const where = `${route} @${width} ${theme}`;
        if (process.env.USABILITY_TRACE) console.log(where);
        await page.goto(route);
        await settle(page);
        if (theme === "dark") expect(await page.evaluate(() => document.documentElement.classList.contains("dark")), "dark theme not applied").toBe(true);
        await shot(page, `${info.project.name}-${width}-${theme}-${route.replace(/[^a-z0-9]+/gi, "_").slice(0, 40) || "home"}`);
        faults.push(...(await layoutFaults(page)).map((f) => `${where}: ${f}`));
        faults.push(...(await rawErrors(page)).map((f) => `${where}: ${f}`));
        // the controls and focus once per page and theme; widths only change layout
        if (width === WIDTHS[info.project.name][0]) {
          faults.push(...(await exercise(page, where)));
          await page.goto(route);
          await settle(page);
          faults.push(...(await focusFaults(page, where)));
          faults.push(...(await layoutFaults(page)).map((f) => `${where} after use: ${f}`));
        }
      }
    }
    // a preview is a dry run that changes nothing; every other acting call is a fault
    expect.soft(w.acted.filter((a) => !/\/api\/actions\/preview$/.test(a)), "an acting endpoint was called").toEqual([]);
    expect.soft([...new Set(w.failed)], "failed API reads").toEqual([]);
    expect.soft([...new Set(w.errors)], "console errors").toEqual([]);
    expect.soft(faults, "usability faults").toEqual([]);
  });
}

test("loading and error states are worded and line up with the page", async ({ page }, info) => {
  test.setTimeout(300_000);
  const w = await watch(page);
  await signIn(page);
  const slow = new Set<string>();
  let fail = false;
  await page.route(/\/api\/(?!auth|me\b|health|session)/, async (r) => {
    if (r.request().method() !== "GET") return r.continue();
    if (fail) return r.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ error: { code: "workbench_busy", message: "The Workbench is busy." } }) });
    slow.add(new URL(r.request().url()).pathname);
    await new Promise((x) => setTimeout(x, 8_000));
    return r.continue().catch(() => {});
  });
  const faults: string[] = [];
  for (const route of ["/my-work", "/late", "/autopilot", "/meetings", "/watching", "/activity", "/team", "/ops", "/rd-projects", "/feedback"]) {
    fail = false;
    await page.goto(route);
    const pending = page.locator("main [data-pending]").first();
    await expect(pending, `${route}: says nothing while it loads`).toBeVisible({ timeout: 15_000 });
    await expect(pending, `${route}: loading text is empty`).toHaveText(/\w{3,}/);
    // the loading line starts where the page's content starts and sits inside it
    const geo = await pending.evaluate((p) => {
      const main = p.closest("main")!.getBoundingClientRect();
      const r = p.getBoundingClientRect();
      const col = (p.parentElement as HTMLElement).getBoundingClientRect();
      const cs = getComputedStyle(p.parentElement as HTMLElement);
      return { left: r.left, colLeft: col.left + parseFloat(cs.paddingLeft), right: r.right, mainRight: main.right, align: getComputedStyle(p).textAlign };
    });
    if (geo.right > geo.mainRight + 1) faults.push(`${route}: loading text runs past the page`);
    if (Math.abs(geo.left - geo.colLeft) > 2 && geo.align !== "center") faults.push(`${route}: loading text starts ${Math.round(geo.left - geo.colLeft)}px off its column`);
    await shot(page, `${info.project.name}-loading-${route.slice(1)}`);
    fail = true;
    await page.goto(route);
    const problem = page.locator("main [role=alert], main [data-testid=server-notice]").first();
    await expect(problem, `${route}: a failed read says nothing`).toBeVisible({ timeout: 15_000 });
    const words = await problem.innerText();
    if (!/\w{3,}/.test(words) || /\{|503|workbench_busy|undefined/.test(words)) faults.push(`${route}: failed-read text "${words.slice(0, 80)}"`);
    if (!(await problem.getByRole("button").count()) && !(await page.locator("main").getByRole("button", { name: /try again|retry/i }).count())) faults.push(`${route}: a failed read offers no retry`);
    faults.push(...(await layoutFaults(page)).map((f) => `${route} error: ${f}`));
    await shot(page, `${info.project.name}-error-${route.slice(1)}`);
  }
  expect(w.acted).toEqual([]);
  expect(faults).toEqual([]);
});

// The autopilot as the owner sees it, with items in several workflows: every filter opens and
// narrows the list; nothing is accepted.
test("autopilot filters open and narrow the list", async ({ page }, info) => {
  test.setTimeout(120_000);
  const w = await watch(page);
  await signIn(page);
  const ME = "33333333-3333-4333-8333-333333333333";
  const item = (id: string, workflow: string, system: string, title: string, today = true) => ({
    item_id: id, autopilot: workflow, label: title, title, workflow, system, roles: ["customer_service"], owner: ME, mine: true,
    happened: { sender: "buyer@customer.example", subject: title, object_id: `Email:x:${id}` },
    prepared: { summary: `Prepared: ${title}` }, citations: ["bc://co/salesOrders/1"],
    due_at: "2026-11-10T17:00:00Z", created_at: "2026-11-10T15:00:00Z", today, state: "waiting", decision: null,
  });
  const area = {
    owner: true,
    switches: { global_off: false, master_switch: false, autopilot_off: false, workflows_off: [] },
    people: [{ id: ME, name: "Ana", you: true, roles: ["customer_service"] }],
    roles: ["customer_service", "receivables"],
    items: [item("i1", "customer_order_intake", "bc", "Customer PO received"), item("i2", "ar_collections", "mail", "Payment question"), item("i3", "teams_chat", "teams", "Teams question", false)],
    workflows: [],
    notes: [],
  };
  await page.route(/\/api\/autopilot$/, (r) => (r.request().method() === "GET" ? r.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(area) }) : r.fallback()));
  await page.goto("/autopilot");
  await settle(page);
  const rows = page.getByTestId("autopilot-row");
  await expect(rows).toHaveCount(2);
  const wf = page.getByLabel("Workflow", { exact: true });
  await wf.selectOption("ar_collections");
  await expect(rows).toHaveCount(1);
  await wf.selectOption("");
  await page.getByLabel("When").selectOption("all");
  await expect(rows).toHaveCount(3);
  await page.getByLabel("System").selectOption("teams");
  await expect(rows).toHaveCount(1);
  expect(await layoutFaults(page)).toEqual([]);
  await shot(page, `${info.project.name}-autopilot-filters`);
  expect(w.acted).toEqual([]);
  expect(w.errors).toEqual([]);
});

// Ask keeps its own scroller: after several questions the follow-up box is still pinned above
// the tab bar and the page itself does not scroll (a phone page that scrolls as a whole must
// not change that).
test("ask keeps the follow-up box pinned through a multi-turn thread", async ({ page }, info) => {
  test.setTimeout(120_000);
  const w = await watch(page);
  await signIn(page);
  await page.goto("/ask");
  await settle(page);
  const questions = ["Who is the supplier on PO 90412?", "When is it due?", "What is still open on it?"];
  for (const [i, q] of questions.entries()) {
    const box = page.getByRole("combobox").last();
    await box.fill(q);
    await box.press("Enter");
    await expect(page.getByTestId("answer-card")).toHaveCount(i + 1);
    await expect(page.getByTestId("answer-card").last()).toHaveAttribute("aria-busy", "false", { timeout: 20_000 });
  }
  await expect(page.getByTestId("answer-card")).toHaveCount(3);
  await page.locator("[data-app-main]").evaluate((m) => { m.scrollTop = 0; });
  const box = page.getByRole("combobox").last();
  const r = await box.evaluate((el) => { const b = el.getBoundingClientRect(); return { bottom: b.bottom, vh: innerHeight, mainScrolls: (document.querySelector("[data-app-main]") as HTMLElement).scrollHeight > (document.querySelector("[data-app-main]") as HTMLElement).clientHeight + 1 }; });
  expect(r.bottom, "follow-up box stays on screen at the top of the thread").toBeLessThanOrEqual(r.vh);
  expect(r.mainScrolls, "the page does not scroll as a whole").toBe(false);
  expect(await layoutFaults(page)).toEqual([]);
  await shot(page, `${info.project.name}-ask-thread`);
  expect(w.acted).toEqual([]);
});
