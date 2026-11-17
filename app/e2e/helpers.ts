import { expect, type Locator, type Page, type Route } from "@playwright/test";

// What every browser pass shares (usability.spec.ts, walk.spec.ts): the controls never pressed,
// the acting endpoints stubbed, the watch on console errors and failed reads, and the checks a
// reader would notice (layout, raw error text, keyboard focus).
/** Controls the pass never presses: anything that sends, confirms, approves, writes or signs out. */
export const NEVER = /\b(send|sent|confirm|approve|accept|execute|perform|undo|switch|stop|pause|resume|go live|sign out|log ?out|delete|remove|reject|snooze|dismiss|mark|save note|submit|prepare the entry)\b/i;

/** Endpoints that act; the browser answers them itself and records the attempt. */
export const ACTING = /\/api\/(actions\/(confirm|preview)|writes(\/[^/]+\/(approve|execute|preview))?$|autopilot\/(items\/[^/]+\/decision|switches|workflows\/[^/]+|audit\/[^/]+\/undo)|meetings\/follow-up\/|my-work\/[^/]+\/write|feedback\/[^/]+\/status)/;

/** Reads that fail by design and that the page turns into words: the mock reader is not the
 * owner (Reliability says it has no access; Trust is not its page) and the mock keeps no meeting
 * follow-ups (the tab says so). Anything else that fails is a fault. */
export const HANDLED = [/ GET \/api\/ops\/status 403$/, / GET \/api\/meetings\/action-items 404$/,
  // Trust is the owner's and the Team readers' page: any other reader is told it is not theirs
  /^\/trust: GET \/api\/trust 404$/];

export type Watch = { errors: string[]; acted: string[]; failed: string[] };

export async function watch(page: Page): Promise<Watch> {
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

export async function signIn(page: Page) {
  await page.goto("/signin");
  // the button waits for the sign-in settings read, which a loaded machine answers slowly
  await page.getByRole("button", { name: "Continue with demo sign-in" }).click({ timeout: 30_000 });
  await expect(page).not.toHaveURL(/\/signin/, { timeout: 20_000 });
}

export async function settle(page: Page) {
  await page.waitForLoadState("networkidle", { timeout: 10_000 }).catch(() => {});
  // every part that said it was loading has finished (or the page shows why not)
  // (30 s: a loaded test machine answers the fixture API slowly; a part that never finishes
  // still fails)
  await expect(page.locator("[data-pending], [aria-busy=true]")).toHaveCount(0, { timeout: 30_000 });
}

/** Layout faults a reader sees: sideways scroll, visible things off screen, cut-off select text, overlapping controls. */
export async function layoutFaults(page: Page): Promise<string[]> {
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
    // only the part of a control its scrolling panes let through can overlap anything (the
    // sidebar's lower lists scroll under its footer, clipped)
    const visible = (e: Element): DOMRect | null => {
      const r = e.getBoundingClientRect();
      let top = r.top;
      let bottom = r.bottom;
      for (let p = e.parentElement; p; p = p.parentElement) {
        if (!/(auto|scroll|hidden|clip)/.test(getComputedStyle(p).overflowY)) continue;
        const pr = p.getBoundingClientRect();
        top = Math.max(top, pr.top);
        bottom = Math.min(bottom, pr.bottom);
        if (bottom - top < 1) return null;
      }
      return new DOMRect(r.left, top, r.width, bottom - top);
    };
    const boxes = controls.filter((e) => !e.closest("[role=menu],[role=dialog],[role=listbox]")).map((e) => [e, visible(e)] as const).filter((b): b is readonly [Element, DOMRect] => b[1] !== null);
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
export async function rawErrors(page: Page): Promise<string[]> {
  const text = await page.locator("body").innerText();
  return [/\{\s*"(code|error|detail)"\s*:/, /\bat \S+ \(.*:\d+:\d+\)/, /Unhandled Runtime Error|TypeError:|SyntaxError:|Traceback|undefined|NaN|\[object Object\]/]
    .filter((re) => re.test(text))
    .map((re) => `raw text on the page: ${re}`);
}

/** Opens and selects every reachable control in `main`; returns what did not respond. */
export async function exercise(page: Page, where: string): Promise<string[]> {
  const out: string[] = [];
  const home = page.url();
  const back = async () => {
    // a drawer that has only just opened does not take Escape yet; nobody presses it that fast
    await page.waitForTimeout(400);
    await page.keyboard.press("Escape");
    // a closing drawer's backdrop still takes clicks for a moment
    const overlay = page.locator(".fixed.inset-0.z-50").first();
    let closed = await overlay.waitFor({ state: "detached", timeout: 3_000 }).then(() => true, () => false);
    // an Escape that a loaded machine dropped while the drawer was still mounting: press it again
    for (let k = 0; !closed && k < 2; k++) {
      await page.keyboard.press("Escape");
      closed = await overlay.waitFor({ state: "detached", timeout: 8_000 }).then(() => true, () => false);
    }
    // an open drawer or menu blocks the page's clicks until it has closed (slow on a loaded
    // machine): wait for the page to take clicks again before the next control
    await page.waitForFunction(() => getComputedStyle(document.body).pointerEvents !== "none", null, { timeout: 10_000 }).catch(() => {});
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
    // a control that changes a stored setting (a switch, a workflow's mode, a note's status)
    // is never pressed: the pass looks, it does not change what the owner set
    if (await b.evaluate(setting).catch(() => true)) continue;
    await clearOfTabBar(b);
    // a click that lands but whose reaction a loaded machine runs slowly is no fault: seconds, not 3
    await b.click({ timeout: 10_000 }).catch((e) => out.push(`${where}: "${label}" cannot be clicked: ${String(e).split("\n").filter((l) => /intercepts|covered|obscur|not stable|not visible/i.test(l)).concat(String(e).split("\n")[0]).join(" | ").slice(0, 400)}`));
    await back();
  }
  return out;
}

/** Whether a button changes a stored setting: a switch, or one choice of a radio group that
 * sets a mode or a status (a view's period or a theme only changes what this screen shows). */
export function setting(el: Element): boolean {
  const role = el.getAttribute("role");
  if (role === "switch") return true;
  if (role !== "radio") return false;
  const group = el.closest("[role=radiogroup]");
  return !group || /\b(mode|status)\b/i.test(group.getAttribute("aria-label") ?? "");
}

/** Brings a control above the phone's fixed tab bar: centred, then, if the page cannot scroll
 * far enough to lift it clear (it sits at the very end), nudged by whatever still overlaps. */
export async function clearOfTabBar(loc: Locator): Promise<void> {
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
export async function focusFaults(page: Page, where: string): Promise<string[]> {
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
