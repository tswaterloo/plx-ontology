import { expect, test, type Locator, type Page, type Route } from "@playwright/test";
import path from "node:path";

// Usability pass in a real browser over operator_api --mock (fixture data): every page at
// 1280 and 1440 (desktop project) and 390 (phone project), light and dark. On each page it
// opens and selects every control it can reach, then checks: no console or page errors,
// nothing scrolls sideways or sits off screen, no select cuts its own chosen text, keyboard
// focus is visible, and loading, empty and error states are worded and aligned with the page.
// Send, confirm, approve, execute, undo and switch endpoints are stubbed and must never be hit.

const shots = process.env.USABILITY_SHOTS || path.join(__dirname, "screenshots", "usability");

// a control that went away fails its read in seconds instead of waiting for the test timeout
test.use({ actionTimeout: 4_000 });

const PAGES = [
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
];

/** Controls the pass never presses: anything that sends, confirms, approves, writes or signs out. */
const NEVER = /\b(send|sent|confirm|approve|accept|execute|perform|undo|switch|stop|pause|resume|go live|sign out|log ?out|delete|remove|reject|snooze|dismiss|mark|save note|submit|prepare the entry)\b/i;

/** Endpoints that act; the browser answers them itself and records the attempt. */
const ACTING = /\/api\/(actions\/(confirm|preview)|writes(\/[^/]+\/(approve|execute|preview))?$|autopilot\/(items\/[^/]+\/decision|switches|workflows\/[^/]+|audit\/[^/]+\/undo)|meetings\/follow-up\/|my-work\/[^/]+\/write|feedback\/[^/]+\/status)/;

/** Reads that fail by design and that the page turns into words: the mock reader is not the
 * owner (Reliability says it has no access) and the mock keeps no meeting follow-ups (the tab
 * says so). Anything else that fails is a fault. */
const HANDLED = [/ GET \/api\/ops\/status 403$/, / GET \/api\/meetings\/action-items 404$/];

type Watch = { errors: string[]; acted: string[]; failed: string[] };

async function watch(page: Page): Promise<Watch> {
  const w: Watch = { errors: [], acted: [], failed: [] };
  page.on("console", (m) => {
    if (m.type() !== "error") return;
    const text = m.text();
    // a failed read is checked by its URL below (the browser's own line has none)
    if (/^Failed to load resource/.test(text)) return;
    w.errors.push(`${new URL(page.url()).pathname}: ${text.slice(0, 240)}`);
  });
  page.on("response", (r) => {
    if (r.status() < 400 || !new URL(r.url()).pathname.startsWith("/api/") || /\/signin/.test(page.url())) return;
    const line = `${new URL(page.url()).pathname}: ${r.request().method()} ${new URL(r.url()).pathname} ${r.status()}`;
    // the stubbed acting endpoints answer 409 and are reported as acted, not as failed reads
    if (!HANDLED.some((h) => h.test(line)) && !ACTING.test(new URL(r.url()).pathname)) w.failed.push(line);
  });
  page.on("pageerror", (e) => w.errors.push(`${new URL(page.url()).pathname}: ${e.message.slice(0, 240)}`));
  await page.route(ACTING, (r: Route) => {
    if (r.request().method() === "GET") return r.continue();
    w.acted.push(`${r.request().method()} ${new URL(r.request().url()).pathname}`);
    return r.fulfill({ status: 409, contentType: "application/json", body: JSON.stringify({ error: { code: "usability_stub", message: "Stubbed in the usability pass." } }) });
  });
  return w;
}

async function signIn(page: Page) {
  await page.goto("/signin");
  await page.getByRole("button", { name: "Continue with demo sign-in" }).click();
  await expect(page).not.toHaveURL(/\/signin/, { timeout: 20_000 });
}

async function settle(page: Page) {
  await page.waitForLoadState("networkidle", { timeout: 10_000 }).catch(() => {});
  // every part that said it was loading has finished (or the page shows why not)
  await expect(page.locator("[data-pending], [aria-busy=true]")).toHaveCount(0, { timeout: 15_000 });
}

/** Layout faults a reader sees: sideways scroll, visible things off screen, cut-off select text, overlapping controls. */
async function layoutFaults(page: Page): Promise<string[]> {
  return page.evaluate(() => {
    const out: string[] = [];
    const vw = window.innerWidth;
    const over = document.documentElement.scrollWidth - vw;
    if (over > 0) out.push(`page scrolls sideways by ${over}px`);
    const shown = (e: Element) => {
      const r = e.getBoundingClientRect();
      const s = getComputedStyle(e);
      if (r.width < 2 || r.height < 2 || s.visibility === "hidden" || s.opacity === "0") return false;
      // inside a closed disclosure the reader sees nothing, though the box keeps a size
      if (e.closest("details:not([open])") && !e.closest("summary")) return false;
      // screen-reader-only text is clipped on purpose
      if (s.position === "absolute" && (s.clip === "rect(0px, 0px, 0px, 0px)" || s.clipPath === "inset(50%)")) return false;
      for (let p: Element | null = e; p; p = p.parentElement) {
        const ps = getComputedStyle(p);
        if (ps.position === "absolute" && (ps.clip === "rect(0px, 0px, 0px, 0px)" || ps.clipPath === "inset(50%)")) return false;
      }
      return true;
    };
    const name = (e: Element) => `${e.tagName.toLowerCase()} "${((e as HTMLElement).innerText || e.getAttribute("aria-label") || "").trim().replace(/\s+/g, " ").slice(0, 40)}"`;
    const controls = Array.from(document.querySelectorAll("a[href], button, select, input, textarea, [role=tab], [role=button]")).filter(shown);
    for (const e of controls) {
      const r = e.getBoundingClientRect();
      // inside a sideways-scrolling strip is fine; on the page itself it is not
      let scroller = false;
      for (let p = e.parentElement; p; p = p.parentElement) {
        const ox = getComputedStyle(p).overflowX;
        if ((ox === "auto" || ox === "scroll") && p.scrollWidth > p.clientWidth) { scroller = true; break; }
      }
      if (!scroller && (r.right > vw + 1 || r.left < -1)) out.push(`${name(e)} is off screen (${Math.round(r.left)}..${Math.round(r.right)} of ${vw})`);
      // a control the pointer cannot reach but the keyboard can is a dead control
      if (getComputedStyle(e).pointerEvents === "none" && !(e as HTMLButtonElement).disabled && (e as HTMLElement).tabIndex >= 0) out.push(`${name(e)} takes focus but ignores clicks`);
    }
    // a select must show its whole chosen option
    const ctx = document.createElement("canvas").getContext("2d")!;
    for (const s of Array.from(document.querySelectorAll("select")).filter(shown) as HTMLSelectElement[]) {
      const cs = getComputedStyle(s);
      ctx.font = `${cs.fontWeight} ${cs.fontSize} ${cs.fontFamily}`;
      const text = s.selectedOptions[0]?.text ?? "";
      const room = s.clientWidth - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight) - (cs.appearance === "none" ? 0 : 20);
      const need = ctx.measureText(text).width;
      if (need > room + 1) out.push(`select "${s.getAttribute("aria-label") ?? s.name}" cuts "${text}" (${Math.round(need)}px in ${Math.round(room)}px)`);
    }
    // two visible controls must not overlap each other (one inside the other is fine)
    const boxes = controls.filter((e) => !e.closest("[role=menu],[role=dialog],[role=listbox]")).map((e) => [e, e.getBoundingClientRect()] as const);
    for (let i = 0; i < boxes.length; i++) {
      for (let j = i + 1; j < boxes.length; j++) {
        const [a, ra] = boxes[i];
        const [b, rb] = boxes[j];
        if (a.contains(b) || b.contains(a)) continue;
        // a fixed or sticky bar (the phone's tab bar, a top bar) rides over scrolled content by design
        const pinned = (e: Element) => { for (let p: Element | null = e; p; p = p.parentElement) { const ps = getComputedStyle(p).position; if (ps === "fixed" || ps === "sticky") return p; } return null; };
        if (pinned(a) !== pinned(b)) continue;
        const x = Math.min(ra.right, rb.right) - Math.max(ra.left, rb.left);
        const y = Math.min(ra.bottom, rb.bottom) - Math.max(ra.top, rb.top);
        if (x > 4 && y > 4) out.push(`${name(a)} overlaps ${name(b)}`);
      }
    }
    return [...new Set(out)].slice(0, 12);
  });
}

/** No raw JSON, stack traces or framework error text anywhere on the page. */
async function rawErrors(page: Page): Promise<string[]> {
  const text = await page.locator("body").innerText();
  return [/\{\s*"(code|error|detail)"\s*:/, /\bat \S+ \(.*:\d+:\d+\)/, /Unhandled Runtime Error|TypeError:|SyntaxError:|Traceback|undefined|NaN|\[object Object\]/]
    .filter((re) => re.test(text))
    .map((re) => `raw text on the page: ${re}`);
}

/** Opens and selects every reachable control in `main`; returns what did not respond. */
async function exercise(page: Page, where: string): Promise<string[]> {
  const out: string[] = [];
  const home = page.url();
  const back = async () => {
    // a drawer that has only just opened does not take Escape yet; nobody presses it that fast
    await page.waitForTimeout(400);
    await page.keyboard.press("Escape");
    // a closing drawer's backdrop still takes clicks for a moment
    await page.locator(".fixed.inset-0.z-50").first().waitFor({ state: "detached", timeout: 3_000 }).catch(() => {});
    if (page.url() !== home) {
      await page.goto(home);
      await settle(page);
    }
  };
  // selects: every option can be chosen and is then the value
  const selects = page.locator("main select:visible");
  for (let i = 0; i < (await selects.count()); i++) {
    const s = selects.nth(i);
    const label = (await s.getAttribute("aria-label")) ?? `select ${i}`;
    if (await s.isDisabled()) {
      out.push(`${where}: select "${label}" is disabled`);
      continue;
    }
    const values = await s.locator("option:not([disabled])").evaluateAll((os) => os.map((o) => (o as HTMLOptionElement).value));
    if (values.length < 2) out.push(`${where}: select "${label}" offers nothing to choose (${values.length} option)`);
    for (const v of values.slice(1).concat(values.slice(0, 1))) {
      await s.selectOption(v);
      if ((await s.inputValue()) !== v) out.push(`${where}: select "${label}" did not take "${v}"`);
    }
    await back();
  }
  // tabs: choosing one selects it
  const tabs = page.locator("main [role=tab]:visible");
  for (let i = 0; i < (await tabs.count()); i++) {
    const t = tabs.nth(i);
    await clearOfTabBar(t);
    await t.click();
    await expect(t, `${where}: tab "${await t.innerText()}" did not select`).toHaveAttribute("aria-selected", "true");
  }
  // menus and disclosures: a click opens them
  // (a combobox opens its list on typing, not on a click)
  const openers = page.locator("main [aria-haspopup]:visible:not([role=combobox]), main [aria-expanded]:visible:not([role=combobox])");
  for (let i = 0; i < (await openers.count()); i++) {
    const o = openers.nth(i);
    const label = ((await o.getAttribute("aria-label")) || (await o.innerText()) || "").trim().slice(0, 40);
    if (NEVER.test(label) || (await o.isDisabled())) continue;
    const h = (await o.elementHandle())!;
    const before = await h.getAttribute("aria-expanded");
    // centre it first: a pinned tab bar covers what sits at the screen edge
    await clearOfTabBar(o);
    const clicked = await o.click().then(() => true, (e) => {
      out.push(`${where}: "${label}" cannot be clicked: ${String(e).split("\n").filter((l) => /intercepts|covered|obscur|not stable|not visible/i.test(l)).concat(String(e).split("\n")[0]).join(" | ").slice(0, 400)}`);
      return false;
    });
    if (!clicked) continue;
    const after = await h.getAttribute("aria-expanded");
    const popup = await page.locator("[role=menu]:visible, [role=dialog]:visible, [role=listbox]:visible").count();
    // a disclosure that replaces itself with what it opens (More) is gone once opened
    const gone = !(await h.evaluate((el) => el.isConnected).catch(() => false));
    if (after === before && popup === 0 && !gone) out.push(`${where}: "${label}" did not open`);
    await back();
  }
  // every other button responds without an error
  const buttons = page.locator("main button:visible");
  const n = Math.min(await buttons.count(), 25);
  for (let i = 0; i < n; i++) {
    const b = buttons.nth(i);
    if (!(await b.isVisible().catch(() => false))) continue;
    const label = ((await b.getAttribute("aria-label")) || (await b.innerText().catch(() => "")) || "").trim().slice(0, 40);
    if (!label) out.push(`${where}: a button has no name`);
    if (NEVER.test(label) || (await b.isDisabled().catch(() => true)) || (await b.getAttribute("role")) === "tab") continue;
    await clearOfTabBar(b);
    await b.click({ timeout: 3_000 }).catch((e) => out.push(`${where}: "${label}" cannot be clicked: ${String(e).split("\n").filter((l) => /intercepts|covered|obscur|not stable|not visible/i.test(l)).concat(String(e).split("\n")[0]).join(" | ").slice(0, 400)}`));
    await back();
  }
  return out;
}

/** Brings a control above the phone's fixed tab bar: centred, then, if the page cannot scroll
 * far enough to lift it clear (it sits at the very end), nudged by whatever still overlaps. */
async function clearOfTabBar(loc: Locator): Promise<void> {
  await loc.evaluate((el) => {
    el.scrollIntoView({ block: "center" });
    const bar = document.querySelector('[data-testid="tab-bar"]');
    if (!bar || getComputedStyle(bar).display === "none") return;
    const top = bar.getBoundingClientRect().top;
    const r = el.getBoundingClientRect();
    // the click lands on the middle of the control: that point must sit above the bar
    const over = Math.min(r.top + r.height / 2, r.bottom) - (top - 8);
    if (over <= 0) return;
    let p: HTMLElement | null = el.parentElement;
    while (p && !(/(auto|scroll)/.test(getComputedStyle(p).overflowY) && p.scrollHeight > p.clientHeight)) p = p.parentElement;
    (p ?? document.scrollingElement)?.scrollBy(0, over);
  }).catch(() => {});
}

/** Tabs through the first controls of the page; each one that takes focus shows it. */
async function focusFaults(page: Page, where: string): Promise<string[]> {
  const out: string[] = [];
  await page.locator("body").click({ position: { x: 1, y: 1 } }).catch(() => {});
  for (let i = 0; i < 14; i++) {
    await page.keyboard.press("Tab");
    const r = await page.evaluate(() => {
      const e = document.activeElement as HTMLElement | null;
      if (!e || e === document.body) return null;
      const s = getComputedStyle(e);
      const ring = (s.outlineStyle !== "none" && parseFloat(s.outlineWidth) > 0) || (s.boxShadow && s.boxShadow !== "none");
      // a row link may draw its ring on the row around it
      let host = false;
      for (let p = e.parentElement, k = 0; p && k < 3; p = p.parentElement, k++) {
        const ps = getComputedStyle(p);
        if (ps.boxShadow && ps.boxShadow !== "none" && p.matches(":has(:focus-visible)")) host = true;
      }
      const shown = e.getBoundingClientRect().width > 0;
      return { ok: !!ring || host || !shown, name: `${e.tagName.toLowerCase()} "${(e.innerText || e.getAttribute("aria-label") || "").trim().slice(0, 30)}"` };
    });
    if (r && !r.ok) out.push(`${where}: focus on ${r.name} is not visible`);
  }
  return [...new Set(out)];
}

async function shot(page: Page, name: string) {
  await page.screenshot({ path: path.join(shots, `${name}.png`), fullPage: true });
}

const WIDTHS: Record<string, number[]> = { desktop: [1440, 1280], phone: [390] };

for (const theme of ["light", "dark"] as const) {
  test(`every page and control works (${theme})`, async ({ page }, info) => {
    test.setTimeout(600_000);
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
    await new Promise((x) => setTimeout(x, 2_500));
    return r.continue().catch(() => {});
  });
  const faults: string[] = [];
  for (const route of ["/my-work", "/late", "/autopilot", "/meetings", "/watching", "/activity", "/team", "/ops", "/rd-projects", "/feedback"]) {
    fail = false;
    await page.goto(route);
    const pending = page.locator("main [data-pending]").first();
    await expect(pending, `${route}: says nothing while it loads`).toBeVisible({ timeout: 5_000 });
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
