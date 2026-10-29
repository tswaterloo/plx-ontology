import { expect, test, type Page } from "@playwright/test";
import path from "node:path";

// The Friday demo flow, end to end, against operator_api --mock (fixture data):
// sign in (DEV) -> home -> typo autocomplete -> messy question -> streaming
// answer with sources -> hidden-items notice -> conflict card -> ambiguous
// follow-up and "did you mean" -> feedback -> object page -> source page.
// Screenshots at desktop and 390 px into e2e/screenshots.

const shots = path.join(__dirname, "screenshots");

async function shot(page: Page, name: string, project: string) {
  // Nothing may scroll sideways, at 390 px or on desktop.
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  expect(overflow, `${name} overflows horizontally by ${overflow}px`).toBeLessThanOrEqual(0);
  await page.screenshot({ path: path.join(shots, `${project}-${name}.png`), fullPage: true });
}

/** No raw JSON, stack traces or framework error text anywhere on the page. */
async function expectNoRawErrors(page: Page) {
  const text = await page.locator("body").innerText();
  expect(text).not.toMatch(/\{\s*"(code|error|detail)"\s*:/);
  expect(text).not.toMatch(/\bat \S+ \(.*:\d+:\d+\)/);
  expect(text).not.toMatch(/Unhandled Runtime Error|TypeError:|SyntaxError:|Traceback/);
}

/** Records every CSP violation on every document the page loads. */
async function watchCsp(page: Page): Promise<string[]> {
  const violations: string[] = [];
  await page.exposeFunction("__cspViolation", (v: string) => violations.push(v));
  await page.addInitScript(() => {
    document.addEventListener("securitypolicyviolation", (e) =>
      (window as unknown as { __cspViolation: (v: string) => void }).__cspViolation(
        `${e.violatedDirective} ${e.blockedURI} ${location.pathname}`,
      ),
    );
  });
  return violations;
}

test("demo walkthrough", async ({ page }, info) => {
  // A long flow on a shared build machine: the budget covers load, not slowness
  // in the app (each step keeps its own 10-20 s expectation timeout).
  test.setTimeout(180_000);
  const project = info.project.name;
  const phone = project === "phone";
  const csp = await watchCsp(page);

  // 1. An access link: the #ticket= fragment leaves the URL on the page it opened.
  //    Signed out -> sign-in page with the DEV banner; Microsoft is off in mock mode.
  const first = await page.goto("/#ticket=demo-ticket-123");
  const policy = first?.headers()["content-security-policy"] ?? "";
  expect(policy).toMatch(/script-src 'self' 'nonce-[^']+' 'strict-dynamic'/);
  expect(policy).toContain("frame-ancestors 'none'");
  expect(policy).not.toContain("unsafe-inline");
  await expect(page).toHaveURL(/\/signin/);
  expect(page.url()).not.toContain("ticket");
  // This mock takes no ticket: the attempt fails in plain words, then the button works.
  await expect(page.getByRole("alert").filter({ hasText: "doesn't take an access link" })).toBeVisible();
  await expect(page.getByTestId("dev-banner")).toBeVisible();
  await expect(page.getByRole("button", { name: /Sign in with Microsoft/ })).toBeDisabled();
  await shot(page, "1-signin", project);
  await page.getByRole("button", { name: "Continue with demo sign-in" }).click();

  // 2. Home: ask box, needs attention (longest late first), the data label in the banner.
  await expect(page).toHaveURL(/\/$/);
  await expect(page.getByRole("heading", { level: 1 })).toContainText("Demo");
  await expect(page.getByTestId("dev-banner")).toContainText("FIXTURE");
  const late = page.getByTestId("late-pos").locator("li");
  await expect(late.first()).toBeVisible();
  const days = (await late.locator("span.tabular-nums").allInnerTexts()).map(Number);
  expect(days.length).toBeGreaterThanOrEqual(2);
  expect([...days].sort((a, b) => b - a)).toEqual(days);
  await shot(page, "2-home", project);

  // 3. Typo-tolerant autocomplete: "mapel" finds Maple Leaf Soapworks.
  const box = page.getByRole("combobox", { name: "Ask a question" });
  await box.click();
  await box.pressSequentially("status of mapel", { delay: 25 });
  await expect(page.getByTestId("suggestions").getByRole("option", { name: /Maple Leaf Soapworks/ })).toBeVisible();
  await shot(page, "3-autocomplete", project);
  await box.press("Escape");
  await box.fill("");

  // 4. A messy question; the answer streams in. An observer records every
  // progressive state the page showed while the answer was still busy.
  await page.evaluate(() => {
    const w = window as unknown as { __plx: { statuses: string[]; partial: boolean } };
    w.__plx = { statuses: [], partial: false };
    new MutationObserver(() => {
      for (const a of document.querySelectorAll('article[aria-busy="true"]')) {
        const st = a.querySelector('[role="status"]')?.textContent?.trim();
        if (st && !w.__plx.statuses.includes(st)) w.__plx.statuses.push(st);
        if (a.querySelector('[data-testid="direct-answer"]')?.textContent) w.__plx.partial = true;
      }
    }).observe(document.body, { subtree: true, childList: true, characterData: true, attributes: true });
  });
  await box.fill("why is the soap order from that canadian guy late");
  await box.press("Enter");
  await expect(page).toHaveURL(/\/ask\?q=/);
  const card = page.getByTestId("answer-card").first();
  const direct = card.getByTestId("direct-answer");
  await expect(card.getByRole("status")).toBeVisible({ timeout: 5_000 });
  await shot(page, "4-streaming", project);
  await expect(card).toHaveAttribute("aria-busy", "false", { timeout: 20_000 });
  await expect(direct).toContainText("12 days late");
  const seen = await page.evaluate(() => (window as unknown as { __plx: { statuses: string[]; partial: boolean } }).__plx);
  // At least two distinct progress steps were shown before the answer landed.
  expect(seen.statuses.length).toBeGreaterThanOrEqual(2);
  expect(seen.statuses.some((t) => t.includes("Searching"))).toBe(true);

  // Sources: chips on facts; hover (or focus) shows the quoted passage.
  const chips = card.getByTestId("source-chip");
  expect(await chips.count()).toBeGreaterThanOrEqual(3);
  const mailChip = chips.filter({ hasText: "Email from Maple Leaf Soapworks" }).first();
  if (phone) await mailChip.focus();
  else await mailChip.hover();
  await expect(page.getByTestId("source-preview")).toContainText("saponification line was down");
  await page.mouse.move(0, 0);
  await page.locator("body").press("Escape");

  // Hidden notice: count and owner only.
  const hidden = card.getByTestId("hidden-notice");
  await expect(hidden).toContainText("3 more items match, but you don't have access.");
  await expect(hidden).toContainText("Ask Finance");

  // Conflict card: both values, the trusted one and why.
  const conflict = card.getByTestId("conflict-card");
  await expect(conflict).toContainText("2026-10-17");
  await expect(conflict).toContainText("2026-10-24");
  await expect(conflict.getByText("Trusted")).toHaveCount(1);
  await expect(conflict).toContainText("system of record");

  await expect(card.getByTestId("not-found")).toContainText("confirmed new ship date");
  await expect(card.getByTestId("freshness")).toContainText("Data as of");
  await card.getByRole("button", { name: "Save this question" }).click();
  const savedKeys = await page.evaluate(() => Object.keys(localStorage).filter((k) => k.startsWith("plx.saved")));
  expect(savedKeys).toEqual(["plx.saved.v2:staff:demo"]);
  await expectNoRawErrors(page);
  await shot(page, "5-answer", project);

  // 5. An ambiguous follow-up: "did you mean", and the pick is sent as object_id with context.
  const followBox = page.getByRole("combobox", { name: "Ask a question" });
  await followBox.fill("which customer is waiting on it");
  await followBox.press("Enter");
  const second = page.getByTestId("answer-card").nth(1);
  await expect(second).toHaveAttribute("aria-busy", "false", { timeout: 20_000 });
  const dym = second.getByTestId("did-you-mean");
  await expect(dym.getByRole("button").first()).toBeVisible();
  const askReq = page.waitForRequest((r) => r.url().endsWith("/api/ask") && r.method() === "POST");
  await dym.getByRole("button", { name: /Glow Botanicals/ }).click();
  const body = (await askReq).postDataJSON() as { object_id?: string; history?: { role: string; text: string }[]; conversation_id?: string };
  expect(body.object_id).toBe("Customer:demo:glow");
  expect(body.conversation_id).toBeTruthy();
  expect(body.history?.length).toBeGreaterThanOrEqual(4);
  expect(body.history?.[0]).toEqual({ role: "user", text: "why is the soap order from that canadian guy late" });
  await expect(page.getByTestId("answer-card").nth(2)).toHaveAttribute("aria-busy", "false", { timeout: 20_000 });

  // 6. Feedback on the first answer: thumbs down with a reason, stored by the API.
  await card.getByRole("button", { name: "No, something was wrong" }).click();
  await card.getByRole("button", { name: "Out of date" }).click();
  await card.getByLabel("Tell us more").fill("ship date changed yesterday");
  const fbRes = page.waitForResponse((r) => r.url().endsWith("/api/feedback"));
  await card.getByRole("button", { name: "Send feedback" }).click();
  const fb = await fbRes;
  expect(fb.status()).toBe(200);
  expect(fb.request().postDataJSON()).toMatchObject({ rating: -1, note: "[Out of date] ship date changed yesterday" });
  expect((await fb.json()).ok).toBe(true);
  await expect(card.getByTestId("feedback-thanks")).toBeVisible();

  // 7. Open the object page from the answer.
  await card.getByTestId("subject-link").filter({ hasText: "PO 90412" }).click();
  await expect(page).toHaveURL(/\/object\/PurchaseOrder\//);
  await expect(page.getByTestId("object-title")).toHaveText("PO 90412");
  await expect(page.getByTestId("object-status")).toContainText("12 days late");
  await expect(page.getByTestId("late-lines")).toContainText("BAR-LAV-100");
  await expect(page.getByTestId("landed-cost")).toContainText("$10,080.00");
  await expect(page.getByTestId("landed-cost")).toContainText("Partial");
  await expect(page.getByTestId("timeline")).toContainText("PO released");
  await expect(page.getByTestId("people")).toContainText("Dana Ortiz");
  await expect(page.getByTestId("hidden-notice")).toContainText("you don't have access");
  await expect(page.getByTestId("conflict-card")).toBeVisible();
  await expectNoRawErrors(page);
  await shot(page, "6-object", project);

  // 8. Dark theme renders the same page.
  await page.emulateMedia({ colorScheme: "dark" });
  await page.reload();
  await expect(page.getByTestId("object-title")).toHaveText("PO 90412");
  await expect(page.locator("html")).toHaveClass(/dark/);
  await shot(page, "7-object-dark", project);
  await page.emulateMedia({ colorScheme: "light" });

  // 9. Open a linked email as a source.
  await page.getByTestId("linked").getByRole("link").first().click();
  await expect(page).toHaveURL(/\/source\?handle=/);
  await expect(page.getByTestId("source-text")).toContainText("saponification");
  await shot(page, "8-source", project);

  // 10. Missing objects and malformed types read as plain language.
  await page.goto("/object/PurchaseOrder/PurchaseOrder%3Ademo%3A00000");
  await expect(page.getByRole("alert").filter({ hasText: "couldn't find that" })).toBeVisible();
  await page.goto("/object/not%20a%20type/1");
  await expect(page.getByText("We couldn't find that page")).toBeVisible();
  await expectNoRawErrors(page);

  // 11. Sign out: back to sign-in, and this person's saved list is gone from the browser.
  await page.goto("/");
  await page.getByRole("button", { name: /^Account:/ }).click();
  await page.getByRole("menuitem", { name: "Sign out" }).click();
  await expect(page).toHaveURL(/\/signin/);
  expect(await page.evaluate(() => Object.keys(localStorage).filter((k) => k.startsWith("plx.saved")))).toEqual([]);

  // Nothing the app rendered was blocked by its own Content-Security-Policy.
  expect(csp).toEqual([]);
});

test("Microsoft sign-in starts under the CSP (MSAL, PKCE, tenant authority)", async ({ page }) => {
  const csp = await watchCsp(page);
  const tenant = "3f1c2b4a-1111-2222-3333-444455556666";
  const authority = `https://login.microsoftonline.com/${tenant}`;
  // The API says: Entra mode. (Only this one public endpoint is replaced.)
  await page.route("**/api/auth/config", (r) =>
    r.fulfill({
      json: {
        mode: "entra",
        dev_login: false,
        sign_in_available: true,
        message: null,
        entra: { authority, tenant_id: tenant, client_id: "00000000-aaaa-bbbb-cccc-000000000000", scopes: ["api://plx-workbench/access"], redirect_uri: null },
      },
    }),
  );
  // Microsoft's endpoints, answered in the browser: no request leaves this machine.
  let authorize: URL | null = null;
  await page.route("https://login.microsoftonline.com/**", (r) => {
    const url = new URL(r.request().url());
    if (url.pathname.includes("openid-configuration")) {
      return r.fulfill({
        json: {
          issuer: `${authority}/v2.0`,
          authorization_endpoint: `${authority}/oauth2/v2.0/authorize`,
          token_endpoint: `${authority}/oauth2/v2.0/token`,
          end_session_endpoint: `${authority}/oauth2/v2.0/logout`,
          jwks_uri: `${authority}/discovery/v2.0/keys`,
        },
      });
    }
    if (url.pathname.includes("discovery/instance")) {
      return r.fulfill({ json: { tenant_discovery_endpoint: `${authority}/v2.0/.well-known/openid-configuration`, metadata: [] } });
    }
    if (url.pathname.endsWith("/oauth2/v2.0/authorize")) {
      authorize = url;
      return r.fulfill({ contentType: "text/html", body: "<p>Microsoft sign-in (test stub)</p>" });
    }
    return r.fulfill({ status: 404, body: "" });
  });

  await page.goto("/signin?next=%2Fask");
  await expect(page.getByTestId("dev-banner")).toHaveCount(0);
  const button = page.getByRole("button", { name: /Sign in with Microsoft/ });
  await expect(button).toBeEnabled();
  await button.click();
  await expect(page.getByText("Microsoft sign-in (test stub)")).toBeVisible();
  expect(authorize).not.toBeNull();
  const q = authorize!.searchParams;
  expect(authorize!.pathname).toBe(`/${tenant}/oauth2/v2.0/authorize`);
  expect(q.get("client_id")).toBe("00000000-aaaa-bbbb-cccc-000000000000");
  expect(q.get("response_type")).toBe("code");
  expect(q.get("code_challenge_method")).toBe("S256");
  expect(q.get("code_challenge")).toBeTruthy();
  expect(q.get("scope")).toContain("api://plx-workbench/access");
  expect(q.get("redirect_uri")).toMatch(/\/signin$/);
  expect(csp).toEqual([]);
});

test("a /common Microsoft authority is refused before MSAL starts", async ({ page }) => {
  let microsoftCalled = false;
  await page.route("https://login.microsoftonline.com/**", (r) => {
    microsoftCalled = true;
    return r.abort();
  });
  await page.route("**/api/auth/config", (r) =>
    r.fulfill({
      json: {
        mode: "entra",
        dev_login: false,
        sign_in_available: true,
        message: null,
        entra: {
          authority: "https://login.microsoftonline.com/common",
          tenant_id: "common",
          client_id: "00000000-aaaa-bbbb-cccc-000000000000",
          scopes: ["api://plx-workbench/access"],
          redirect_uri: null,
        },
      },
    }),
  );
  await page.goto("/signin");
  await expect(page.getByRole("alert").filter({ hasText: "isn't set up correctly" })).toBeVisible();
  await expect(page.getByRole("button", { name: /Sign in with Microsoft/ })).toHaveCount(0);
  expect(microsoftCalled).toBe(false);
});
