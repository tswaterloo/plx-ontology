import { expect, test, type Cookie, type Page, type Route } from "@playwright/test";
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { layoutFaults, rawErrors, settle, signIn, watch, type Watch } from "./helpers";

// A walk through the whole app as a person would take it, once per kind of reader: the owner,
// an admin (a Team reader who triages feedback), a team member (purchasing) and a plain reader.
// From the nav it opens every section and place the reader has, then follows the links it finds
// into lists, tabs and detail pages, a few of each kind. On every page it checks what a reader
// would notice: console and page errors, failed reads, raw error text, a "no page here" where a
// link pointed, layout faults at the width, copy with the tics tests/test_ui_copy_tics.py bans,
// and that memory, templates and the logs are one click away. It also opens every old address
// (each must land on its new page), empties every list the server sends (each page must say so
// in words) and walks the sidebar by keyboard. The reader's role is set on /api/me in the
// browser: what the nav and pages show by role is checked here, what the server refuses by role
// is the Python tests' (tests/test_app_api.py). Nothing that sends, confirms or writes is pressed;
// acting endpoints are stubbed (helpers.ts ACTING) and any attempt fails the walk.

const out = process.env.WALK_OUT || path.join(__dirname, "screenshots", "walk");
const MAX_PAGES = Number(process.env.WALK_MAX_PAGES || 70);
const PER_KIND = Number(process.env.WALK_PER_KIND || 2);
/** The live app (E2E_BASE_URL, with the owner's access ticket in E2E_TICKET): the real owner walks,
 * then views as each person LIVE_VIEW_AS names (the app's own read-only view-as, audited). Read
 * only: every request but sign-in and the view-as marks that is not a GET is refused in the browser,
 * so nothing is sent, approved, marked read or saved; links that would run an Ask are not followed. */
const LIVE = !!process.env.E2E_BASE_URL;
const VIEW_AS = (process.env.LIVE_VIEW_AS ?? "").split(",").map((v) => v.trim()).filter(Boolean);
/** Each role walk runs once per theme (WALK_THEMES, default both): the theme is set the way the
 * app's switch stores it, and the walk checks the page took it. WALK_SHOTS=1 keeps a full-page
 * screenshot of every page walked (shots/<walk>-<project>-<theme>/), the after-shots of a walk. */
type Theme = "light" | "dark";
const THEMES = ((process.env.WALK_THEMES ?? "light,dark").split(",").map((t) => t.trim()).filter((t) => t === "light" || t === "dark")) as Theme[];
const SHOTS = process.env.WALK_SHOTS === "1";

type Role = "owner" | "admin" | "member" | "reader";
const ROLES: Record<Role, Record<string, unknown>> = {
  owner: { owner: true, ops_reader: true, feedback_admin: true, can_view_as: true, access_label: "Full read", roles: [] },
  admin: { owner: false, ops_reader: true, feedback_admin: true, can_view_as: false, access_label: "Full read", roles: ["purchasing"] },
  member: { owner: false, ops_reader: false, feedback_admin: false, can_view_as: false, access_label: null, roles: ["purchasing"] },
  reader: { owner: false, ops_reader: false, feedback_admin: false, can_view_as: false, access_label: null, roles: [] },
};
const WALK_ROLES = (process.env.WALK_ROLES?.split(",") as Role[] | undefined) ?? (Object.keys(ROLES) as Role[]);

/** The owner's nav decision (11-14): six sections plus Records; Admin for the owner and admins. */
const SECTIONS_ALL = ["My work", "Ask", "Watching", "Autopilot", "Meetings", "Team", "Records", "Admin"];
const SECTIONS: Record<Role, string[]> = { owner: SECTIONS_ALL, admin: SECTIONS_ALL, member: SECTIONS_ALL.slice(0, 7), reader: SECTIONS_ALL.slice(0, 7) };
/** Places only some readers have, and who has them. */
const GATED: Record<string, Role[]> = { "/ops": ["owner", "admin"], "/trust": ["owner", "admin"], "/feedback": ["owner", "admin"] };
/** Each section's own page: memory, templates and logs must be a click away from every one. */
const SECTION_PAGES = ["/", "/ask", "/watching", "/autopilot", "/meetings", "/records", "/ops"];
const EVERYWHERE = [/^\/memory\b/, /^\/templates\b/, /^\/work-log\b/];

/** Every page route, and record pages the fixture holds, so nothing waits on a link to it. */
const SEEDS = [
  "/", "/my-work", "/activity", "/ask", "/memory", "/templates", "/help", "/watching", "/late",
  "/watching#data-health", "/autopilot", "/autopilot?tab=audit", "/meetings", "/meetings?view=weekly", "/meetings?view=follow-ups", "/work-log", "/team", "/records",
  "/ops", "/trust", "/feedback",
  "/object/PurchaseOrder/PurchaseOrder%3Ademo%3A90412", "/object/SalesOrder/SalesOrder%3Ademo%3A77120",
  "/po/90412", "/customer/glow", "/meetings/brief/Customer/Customer%3Ademo%3Aglow", "/source?handle=bc%3A%2F%2Fdemo%2FpurchaseOrders%2F90412",
];

/** Old addresses and where each now lands (next.config.ts redirects; nothing removed). */
const OLD: [string, string][] = [
  ["/briefing", "/watching#alerts"],
  ["/prepare", "/meetings"],
  ["/weekly", "/meetings?view=weekly"],
  ["/brief/Customer/Customer%3Ademo%3Aglow", "/meetings/brief/Customer/Customer%3Ademo%3Aglow"],
  ["/approvals", "/team#approvals"],
  ["/rules", "/watching"],
  ["/actions", "/activity"],
  ["/replies", "/?view=reply"],
  ["/workload", "/team#workload"],
  ["/labour", "/team#labour"],
  ["/meetings/action-items", "/meetings?view=follow-ups"],
  ["/pack/PurchaseOrder/PurchaseOrder%3Ademo%3A90412", "/object/PurchaseOrder/PurchaseOrder%3Ademo%3A90412#documents"],
  ["/o/PurchaseOrder/PurchaseOrder%3Ademo%3A90412", "/object/PurchaseOrder/PurchaseOrder%3Ademo%3A90412"],
];

/** Reads a page turns into words by design: the mock keeps no meeting follow-ups, a reader
 * without the owner's grant is told Reliability and Trust are not theirs. */
const HANDLED = [/ GET \/api\/meetings\/action-items 404$/, / GET \/api\/work-log\/threads\/\S+ 404$/, / GET \/api\/labour 403$/, / GET \/api\/(ops\/status|ops\/[a-z-]+|trust|feedback[^ ]*|autopilot\/audit|workload\/grants|view-as[^ ]*) 403$/, / GET \/api\/trust 404$/];

/** The kind of page a link opens, so the walk opens a few of each and not every record. */
function kindOf(href: string): string {
  const u = new URL(href, "http://x");
  const p = u.pathname
    .replace(/^\/(object|meetings\/brief)\/[^/]+\/[^/]+$/, "/$1/:type/:id")
    .replace(/^\/(po|ao|customer|supplier)\/[^/]+$/, "/$1/:ref")
    .replace(/^\/meetings\/(?!brief)[^/]+$/, "/meetings/:id");
  return `${p}?${[...u.searchParams.keys()].sort().join("&")}`;
}

/** What the server refuses a reader who is neither the owner nor a Team reader (operator_api/app.py):
 * the mock signs in one reader, made an ops reader and feedback admin for the walk (run with
 * PLX_TEAM_VIEW_READERS and PLX_FEEDBACK_ADMINS naming it), so a lesser role gets these answers here. */
const REFUSED: [RegExp, number][] = [
  [/\/api\/trust$/, 404], [/\/api\/ops\//, 403], [/\/api\/autopilot\/audit/, 403], [/\/api\/workload\/grants/, 403],
  [/\/api\/feedback(\?|$)/, 403], [/\/api\/view-as\//, 403], [/\/api\/labour/, 403],
];

async function asRole(page: Page, role: Role) {
  if (role === "member" || role === "reader") {
    for (const [re, status] of REFUSED) {
      await page.route(re, (r: Route) => r.request().method() !== "GET" ? r.fallback()
        : r.fulfill({ status, contentType: "application/json", body: JSON.stringify({ error: { code: status === 404 ? "not_found" : "forbidden", message: status === 404 ? "Not found." : "Not yours to open." } }) }));
    }
  }
  await page.route("**/api/me", async (r: Route) => {
    const res = await r.fetch();
    if (!res.ok()) return r.fulfill({ response: res });
    const me = await res.json();
    return r.fulfill({ response: res, json: { ...me, ...ROLES[role] } });
  });
}

let liveSession: Cookie[] | null = null;
/** Signs in: the access ticket on the live app, the demo sign-in on the fixture. */
async function enter(page: Page) {
  if (!LIVE) return signIn(page);
  const ticket = process.env.E2E_TICKET;
  if (!ticket) throw new Error("E2E_TICKET is not set (the owner's access ticket; never printed)");
  await page.route(/\/api\//, (r: Route) => {
    const req = r.request();
    const p = new URL(req.url()).pathname;
    if (req.method() === "GET" || req.method() === "HEAD" || /^\/api\/(auth\/|view-as\/(start|stop)$)/.test(p)) return r.fallback();
    return r.fulfill({ status: 409, contentType: "application/json", body: JSON.stringify({ error: { code: "read_only_walk", message: "The walk reads only." } }) });
  });
  // the ticket is redeemed once (a second redeem is refused 403): later tests in this run reuse
  // the session it opened. E2E_SESSION_FILE (0600, the runner deletes it) keeps that session
  // for the worker Playwright starts after a failed test and for walks run beside this one;
  // without it the session is kept in memory only (one worker, one module for the whole run)
  const file = process.env.E2E_SESSION_FILE;
  if (!liveSession && file && fs.existsSync(file)) liveSession = JSON.parse(fs.readFileSync(file, "utf8")) as Cookie[];
  if (liveSession) {
    await page.context().addCookies(liveSession);
    await page.goto("/");
  } else {
    // the app redeems the ticket a moment after the page loads: wait for that answer, or the
    // cookies kept below are the signed-out ones and every later test lands on sign-in
    const redeemed = page.waitForResponse((r) => r.url().endsWith("/api/auth/dev-login") && r.request().method() === "POST", { timeout: 180_000 });
    await page.goto(`/#ticket=${encodeURIComponent(ticket)}`);
    const r = await redeemed;
    if (!r.ok()) throw new Error(`the access ticket was refused (${r.status()}): mint a fresh link`);
  }
  await expect(page).not.toHaveURL(/\/signin/, { timeout: 60_000 });
  if (!liveSession) {
    liveSession = await page.context().cookies();
    if (file) fs.writeFileSync(file, JSON.stringify(liveSession), { mode: 0o600 });
  }
}

/** What a page still shows as loading: each part's own name, so a slow read is named in the walk's faults. */
async function pending(page: Page): Promise<string> {
  const names = await page.locator("[data-pending], [aria-busy=true]").evaluateAll((es) => es.map((e) =>
    e.getAttribute("data-pending") || e.getAttribute("aria-label") || e.getAttribute("data-testid") || (e.textContent ?? "").trim().slice(0, 40) || e.tagName.toLowerCase()));
  return names.join(", ") || "nothing named";
}

type Visit = { url: string; landed: string; faults: string[]; text: string; links: string[] };

/** One page: open it, let it settle, and read what a reader would notice. */
async function visit(page: Page, url: string, w: Watch, width: number): Promise<Visit> {
  const before = { e: w.errors.length, f: w.failed.length, a: w.acted.length };
  const faults: string[] = [];
  const res = await page.goto(url).catch((e) => (faults.push(`did not open: ${String(e).split("\n")[0]}`), null));
  if (res && res.status() >= 400) faults.push(`answered ${res.status()}`);
  await settle(page).catch(async () => faults.push(`still loading after 40 s: ${await pending(page)}`));
  const landed = new URL(page.url()).pathname + new URL(page.url()).search + new URL(page.url()).hash;
  // a page with no <main> (a file, Caddy's own page) never answers a wait for one: 15 s, then "nothing"
  const text = await page.locator("main").innerText({ timeout: 15_000 }).catch(() => "");
  if (/There is no page at this address/.test(text)) faults.push("shows 'no page at this address'");
  if (text.replace(/\s+/g, " ").trim().split(" ").length < 3) faults.push("main says nothing");
  if (/could not be read|couldn.t be read/i.test(text) && !/try again|retry/i.test(text)) faults.push("a failed read with no way to retry");
  // the page's own checks run in the page: a page too heavy to answer them in a minute is itself a fault
  const checked = await Promise.race([
    Promise.all([rawErrors(page), layoutFaults(page)]),
    new Promise<null>((res) => setTimeout(() => res(null), 60_000)),
  ]).catch(() => null);
  if (!checked) faults.push("the page did not answer its checks in 60 s (too heavy, or frozen)");
  else faults.push(...checked[0], ...checked[1].map((f) => `${width}px: ${f}`));
  faults.push(...w.errors.slice(before.e), ...w.failed.slice(before.f).filter((l) => !HANDLED.some((h) => h.test(l))), ...w.acted.slice(before.a).map((a) => `ACTED ${a}`));
  const links = await page.locator("a[href^='/']").evaluateAll((as) => as.map((a) => a.getAttribute("href") ?? "").filter((h) => !h.startsWith("/api/")));
  return { url, landed, faults: [...new Set(faults)], text, links: [...new Set(links)] };
}

/** Copy tics per line of page text, through operating/prose_style.py (tests/test_ui_copy_tics.py's rules). */
function copyTics(pages: { url: string; text: string }[]): string[] {
  const py = process.env.PLX_API_PYTHON || "python3";
  const code = [
    "import json, re, sys",
    "from operating.prose_style import tics",
    // a cut list ("A, B and 3 more"); a count of what the reader may not open is not one
    "cut = re.compile(r'(?:,|;|\\band)\\s*\\d+\\s+more\\b(?!\\s+(?:you can.t|withheld|held back))')",
    "out = []",
    "for p in json.load(sys.stdin):",
    "    for line in {l.strip() for l in p['text'].splitlines() if len(l.strip()) > 12}:",
    "        found = tics(line) + (['truncated list'] if cut.search(line) else [])",
    "        if found: out.append(p['url'] + ': ' + ', '.join(dict.fromkeys(found)) + ': ' + line[:120])",
    "print(json.dumps(sorted(set(out))))",
  ].join("\n");
  const got = execFileSync(py, ["-c", code], { cwd: path.join(__dirname, "..", ".."), input: JSON.stringify(pages), encoding: "utf8" });
  return JSON.parse(got);
}

function save(name: string, data: unknown) {
  fs.mkdirSync(out, { recursive: true });
  fs.writeFileSync(path.join(out, `${name}.json`), JSON.stringify(data, null, 1));
}

/** The nav as this reader sees it: sidebar section names on a desk, the tab bar and More on a phone. */
async function navPlaces(page: Page, phone: boolean): Promise<{ sections: string[]; hrefs: string[] }> {
  if (!phone) {
    const sections = await page.locator("[data-testid=nav-sections] > a").evaluateAll((as) => as.map((a) => (a.textContent ?? "").replace(/\d+$/, "").trim()));
    const hrefs = await page.locator("aside a[href^='/']").evaluateAll((as) => as.map((a) => a.getAttribute("href") ?? ""));
    return { sections, hrefs };
  }
  const bar = page.getByTestId("tab-bar");
  const tabs = await bar.locator("a").evaluateAll((as) => as.map((a) => (a.textContent ?? "").replace(/\d+$/, "").trim()));
  await page.getByTestId("tab-more").click();
  const menu = page.getByTestId("more-menu");
  await expect(menu).toBeVisible();
  const more = await menu.locator("a, [role=menuitem]").evaluateAll((as) => as.map((a) => (a.textContent ?? "").replace(/\d+$/, "").trim()));
  const hrefs = await page.locator("[data-testid=tab-bar] a[href^='/'], [data-testid=more-menu] a[href^='/']").evaluateAll((as) => as.map((a) => a.getAttribute("href") ?? ""));
  await page.keyboard.press("Escape");
  return { sections: [...tabs, ...more], hrefs };
}

// live: a view-as walk that fails part way still ends its view-as, so the next walk is the owner's
test.afterEach(async ({ page }) => {
  if (!LIVE) return;
  const key = await page.evaluate(() => JSON.parse(sessionStorage.getItem("plx-view-as") ?? "null")?.key ?? null).catch(() => null);
  if (key) await page.request.post("/api/view-as/stop", { data: { key } }).catch(() => undefined);
});

const WALKS: { role: Role; as?: string }[] = LIVE ? [{ role: "owner" }, ...VIEW_AS.map((as) => ({ role: "owner" as Role, as }))] : WALK_ROLES.map((role) => ({ role }));
for (const theme of THEMES) for (const { role, as } of WALKS) {
  test(`walk as ${as ? `the owner viewing as ${as}` : role} (${theme})`, async ({ page }, info) => {
    test.setTimeout(45 * 60_000);
    const phone = info.project.name === "phone";
    const width = page.viewportSize()?.width ?? 0;
    const name = `walk-${as ? `as-${as.toLowerCase()}` : role}-${info.project.name}-${theme}`;
    await page.emulateMedia({ colorScheme: theme });
    await page.addInitScript((t) => { try { localStorage.setItem("theme", t); } catch { /* storage off: the system theme is emulated too */ } }, theme);
    const w = await watch(page);
    if (!LIVE) await asRole(page, role);
    await enter(page);
    if (as) {
      const people = (await (await page.request.get("/api/view-as/people")).json()) as { people?: { key: string; name: string }[] };
      const who = (people.people ?? []).find((p) => p.name.toLowerCase().startsWith(as.toLowerCase()));
      test.skip(!who, `${as} is not one the owner may view as`);
      await page.request.post("/api/view-as/start", { data: { key: who!.key } });
      await page.addInitScript((v) => sessionStorage.setItem("plx-view-as", JSON.stringify(v)), { key: who!.key, name: who!.name });
      await page.goto("/");
      await expect(page.getByTestId("view-as-bar")).toBeVisible({ timeout: 30_000 });
    }
    const report: Record<string, unknown> = { role, project: info.project.name, theme };
    const faults: string[] = [];
    // a slow first page is a fault of that page, not the end of the walk
    await settle(page).catch(async () => faults.push(`first page still loading after 40 s: ${await pending(page)}`));
    const themed = await page.evaluate(() => document.documentElement.classList.contains("dark"));
    if (themed !== (theme === "dark")) faults.push(`theme: asked for ${theme}, the page shows ${themed ? "dark" : "light"}`);

    const nav = await navPlaces(page, phone);
    report.nav = nav;
    // viewing as someone, the nav is still the owner's own; the person's pages are what is walked
    if (!as) for (const s of SECTIONS[role]) if (!nav.sections.some((n) => n === s || n.startsWith(s))) faults.push(`nav: section "${s}" missing`);
    if (!as) for (const n of SECTIONS_ALL) if (!SECTIONS[role].includes(n) && nav.sections.includes(n)) faults.push(`nav: section "${n}" shown to a ${role}`);
    for (const [href, who] of Object.entries(GATED)) {
      const shown = nav.hrefs.some((h) => h.split(/[?#]/)[0] === href);
      if (shown && !who.includes(role)) faults.push(`nav: ${href} shown to a ${role}`);
    }

    // the walk: seeds first, then the links each page offers, a few of each kind
    const seen = new Map<string, number>();
    // the fixture's records do not exist on the live app: there the walk finds records by their links
    const queue = [...nav.hrefs.filter((h) => h && !h.includes("#")), ...SEEDS.filter((u) => !LIVE || !/demo|90412|glow/.test(u))];
    const visits: Visit[] = [];
    while (queue.length && visits.length < MAX_PAGES) {
      const url = queue.shift()!;
      const k = kindOf(url);
      if ((seen.get(k) ?? 0) >= PER_KIND || visits.some((v) => v.url === url)) continue;
      seen.set(k, (seen.get(k) ?? 0) + 1);
      const v = await visit(page, url, w, width);
      visits.push(v);
      if (SHOTS) {
        const slug = url.replace(/^\//, "").replace(/[^a-zA-Z0-9]+/g, "_").slice(0, 80) || "home";
        fs.mkdirSync(path.join(out, "shots", name), { recursive: true });
        await page.screenshot({ path: path.join(out, "shots", name, `${String(visits.length).padStart(2, "0")}-${slug}.png`), fullPage: true, timeout: 60_000 }).catch(() => {});
      }
      const gate = GATED[url.split(/[?#]/)[0]];
      // a gated page opened by its address: a plain refusal in words, never its content
      if (gate && !gate.includes(role) && !/There is no page at this address|not (yours|available|open to you)|no access|only for|owner|ask .* for access|can.t open|isn.t yours/i.test(v.text)) v.faults.push(`gated page opened for a ${role} without a refusal: "${v.text.slice(0, 80).replace(/\s+/g, " ")}"`);
      // fixture mode: the mock reader is no ops reader, so Trust is the 404 it gives anyone else
      // (the owner's Trust is walked on the live app); /desktop/ is Caddy's, not the app's
      if (!process.env.E2E_BASE_URL) v.faults = v.faults.filter((f) => !((url === "/trust" && /no page at this address|answered 404/.test(f)) || (url.startsWith("/desktop") && /no page|answered 404/.test(f))));
      if (SECTION_PAGES.includes(url)) {
        // on a phone the nav is the tab bar and More, on every page
        const all = phone ? [...nav.hrefs] : [];
        all.push(...(await page.locator("a[href^='/']").evaluateAll((as) => as.filter((a) => (a as HTMLElement).offsetParent !== null || getComputedStyle(a).position === "fixed").map((a) => a.getAttribute("href") ?? ""))));
        for (const re of EVERYWHERE) if (!all.some((h) => re.test(h))) v.faults.push(`no link to ${re.source.slice(2, -2)} from the ${url} section`);
      }
      // a linked document the reader may not open is a count in words, never a title
      if (url === "/object/PurchaseOrder/PurchaseOrder%3Ademo%3A90412") {
        const line = await page.getByTestId("linked-withheld").innerText().catch(() => "");
        if (!/^Not listed: 1 linked document you can.t open\.$/.test(line.trim())) v.faults.push(`withheld linked document line reads "${line}"`);
      }
      for (const l of v.links) if (!l.startsWith("/signin") && !(LIVE && /^\/ask\?(.*&)?q=/.test(l)) && !visits.some((x) => x.url === l)) queue.push(l);
    }
    // every link seen that the walk did not open still has to answer
    const linked = [...new Set(visits.flatMap((v) => v.links))].filter((l) => !visits.some((v) => v.url === l) && !l.startsWith("/signin") && (!!process.env.E2E_BASE_URL || !l.startsWith("/desktop")));
    const dead: string[] = [];
    for (const l of linked.slice(0, 200)) {
      const r = await page.request.get(l, { maxRedirects: 5 }).catch(() => null);
      if (!r || r.status() >= 400) dead.push(`${l} -> ${r?.status() ?? "no answer"}`);
    }
    for (const v of visits) for (const f of v.faults) faults.push(`${v.url}${v.landed !== v.url ? ` (at ${v.landed})` : ""}: ${f}`);
    faults.push(...dead.map((d) => `dead link ${d}`));
    const tics = copyTics(visits.map((v) => ({ url: v.url, text: v.text })));
    faults.push(...tics.map((t) => `copy tic: ${t}`));
    report.pages = visits.map((v) => ({ url: v.url, landed: v.landed, faults: v.faults, head: v.text.slice(0, 160) }));
    report.kinds = [...seen.keys()];
    report.links_checked = linked.length;
    report.faults = faults;
    if (as) {
      const key = await page.evaluate(() => JSON.parse(sessionStorage.getItem("plx-view-as") ?? "null")?.key ?? null);
      if (key) await page.request.post("/api/view-as/stop", { data: { key } });
      report.view_as = as;
    }
    save(name, report);
    expect.soft(visits.length, "pages walked").toBeGreaterThan(15);
    expect(faults, `faults walking as ${role} (${visits.length} pages, ${seen.size} kinds, ${linked.length} more links checked)`).toEqual([]);
  });
}

test("every old address lands on its new page", async ({ page }, info) => {
  test.skip(info.project.name === "phone", "addresses do not depend on the width");
  await enter(page);
  const wrong: string[] = [];
  for (const [from, to] of OLD) {
    await page.goto(from);
    await page.waitForLoadState("domcontentloaded");
    const u = new URL(page.url());
    const want = new URL(to, u.origin);
    if (u.pathname !== want.pathname || u.search !== want.search || u.hash !== want.hash) wrong.push(`${from} -> ${u.pathname}${u.search}${u.hash} (want ${to})`);
    // the fixture's records only exist on the fixture
    if (!LIVE && (await page.getByText("There is no page at this address").isVisible().catch(() => false))) wrong.push(`${from}: no page`);
  }
  expect(wrong).toEqual([]);
});

test("every list empty: each page says so in words", async ({ page }, info) => {
  test.setTimeout(10 * 60_000);
  const width = page.viewportSize()?.width ?? 0;
  const w = await watch(page);
  if (!LIVE) await asRole(page, "owner");
  await enter(page);
  // every array the server sends comes back empty (who is reading and sign-in excepted)
  const empty = (v: unknown): unknown => (Array.isArray(v) ? [] : v && typeof v === "object" ? Object.fromEntries(Object.entries(v).map(([k, x]) => [k, empty(x)])) : v);
  await page.route(/\/api\/(?!me$|auth\/|health)/, async (r) => {
    if (r.request().method() !== "GET") return r.fallback();
    const res = await r.fetch();
    const type = res.headers()["content-type"] ?? "";
    if (!res.ok() || !type.includes("json")) return r.fulfill({ response: res });
    return r.fulfill({ response: res, json: empty(await res.json()) });
  });
  const faults: string[] = [];
  // fixture mode: Trust is the mock reader's 404 (walked on the live app)
  // live: the fixture's records (demo, PO 90412, Glow) do not exist there (as the walk's queue)
  for (const url of SEEDS.filter((s) => !s.includes("%3A") && !s.includes("?") && (s !== "/trust" || !!process.env.E2E_BASE_URL) && (!LIVE || !/demo|90412|glow/.test(s)))) {
    const v = await visit(page, url, w, width);
    // a page that crashed on an empty list shows the error boundary or nothing at all
    if (/Something went wrong|This page could not load/i.test(v.text)) v.faults.push("crashed on empty lists");
    for (const f of v.faults) faults.push(`${url}: ${f}`);
  }
  save(`empty-${info.project.name}`, faults);
  expect(faults).toEqual([]);
});

test("keyboard: skip link, the sidebar, the palette and Escape", async ({ page }, info) => {
  test.skip(info.project.name === "phone", "a phone has no keyboard walk");
  if (!LIVE) await asRole(page, "owner");
  await enter(page);
  await settle(page);
  const faults: string[] = [];
  // from a fresh load, as a keyboard user arrives
  await page.goto("/");
  await settle(page);
  await page.keyboard.press("Tab");
  const first = await page.evaluate(() => (document.activeElement as HTMLElement | null)?.innerText?.trim() ?? "");
  if (!/skip to content/i.test(first)) faults.push(`first Tab stop is "${first.slice(0, 40)}", not the skip link`);
  await page.keyboard.press("Enter");
  const inMain = await page.evaluate(() => !!document.activeElement?.closest("main") || document.activeElement?.id === "main" || location.hash === "#main");
  if (!inMain) faults.push("skip link does not move focus to the page");
  // every sidebar link takes focus visibly, in order, and Enter opens it
  const sideLinks = await page.locator("aside a[href]").count();
  await page.locator("aside a[href]").first().focus();
  const stops: string[] = [];
  for (let i = 0; i < sideLinks + 4; i++) {
    const r = await page.evaluate(() => {
      const e = document.activeElement as HTMLElement | null;
      if (!e || !e.closest("aside")) return null;
      const s = getComputedStyle(e);
      const ring = (s.outlineStyle !== "none" && parseFloat(s.outlineWidth) > 0) || (s.boxShadow && s.boxShadow !== "none");
      return { name: (e.innerText || e.getAttribute("aria-label") || "").trim().slice(0, 30), ring: !!ring };
    });
    if (!r) break;
    stops.push(r.name);
    if (!r.ring) faults.push(`sidebar: focus on "${r.name}" is not visible`);
    await page.keyboard.press("Tab");
  }
  if (stops.length < 6) faults.push(`sidebar takes only ${stops.length} Tab stops`);
  await page.locator("aside a[href='/watching']").first().focus();
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(/\/watching/);
  // the palette opens on Ctrl+K and on "/", closes on Escape and gives focus back
  for (const key of ["Control+k", "/"]) {
    await page.evaluate(() => (document.activeElement as HTMLElement | null)?.blur());
    await page.keyboard.press(key);
    const dialog = page.getByRole("dialog");
    if (!(await dialog.waitFor({ state: "visible", timeout: 8_000 }).then(() => true, () => false))) { faults.push(`${key} does not open the palette`); continue; }
    await page.keyboard.type("late");
    await page.keyboard.press("Escape");
    if (await dialog.isVisible().catch(() => false)) faults.push(`Escape does not close the palette (opened by ${key})`);
  }
  expect(faults).toEqual([]);
});

test("narrow widths: tablet and small laptop", async ({ page }, info) => {
  test.skip(info.project.name === "phone", "the phone project is the narrow one");
  test.setTimeout(10 * 60_000);
  const w = await watch(page);
  if (!LIVE) await asRole(page, "owner");
  await enter(page);
  const faults: string[] = [];
  for (const width of [768, 1024, 1180]) {
    await page.setViewportSize({ width, height: 900 });
    for (const url of ["/", "/ask", "/watching", "/late", "/autopilot", "/meetings", "/team", "/records", "/memory", "/templates", "/work-log", "/ops", ...(LIVE ? [] : ["/object/PurchaseOrder/PurchaseOrder%3Ademo%3A90412"])]) {
      const v = await visit(page, url, w, width);
      for (const f of v.faults) faults.push(`${url}: ${f}`);
      // the places under a section must be reachable at this width too
      if (url === "/watching") {
        const late = await page.locator("a[href='/late']").evaluateAll((as) => as.some((a) => a.getBoundingClientRect().width > 0));
        if (!late) faults.push(`${width}px: no visible way from Watching to Late POs`);
      }
    }
  }
  save("narrow", faults);
  expect(faults).toEqual([]);
});
