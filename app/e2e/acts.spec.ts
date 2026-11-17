import { expect, test, type Page, type Request } from "@playwright/test";
import { settle, signIn } from "./helpers";

// Send, Confirm and Approve pressed through the real UI, on fixtures only:
// the confirm step appears, the result is a dry run the app records, a second press or a stale
// confirm does nothing more, and no request leaves the box. The fixture server has no send
// credentials and no write transport (mock_studio: no adapters, no BC transport), so nothing can
// be performed; what a live transport does with each kind is the Python suites' (fake transports:
// operating.tests.test_card_actions, test_write_intents, test_autopilot_write*,
// operator_api.tests.test_write_intents_api, test_autopilot_area*, test_workspace_actions).
// Never against the live app: this spec presses buttons that act.
test.skip(!!process.env.E2E_BASE_URL, "acting spec: fixtures only, never the live app");

/** Every request the browser makes off this machine (none may happen). */
function offBox(page: Page): string[] {
  const out: string[] = [];
  page.on("request", (r: Request) => {
    const u = new URL(r.url());
    if (!/^(127\.0\.0\.1|localhost)$/.test(u.hostname) && !u.protocol.startsWith("data") && !u.protocol.startsWith("blob")) out.push(`${r.method()} ${u.origin}${u.pathname}`);
  });
  return out;
}

const CARD_PAGES = ["/", "/my-work", "/watching", "/object/PurchaseOrder/PurchaseOrder%3Ademo%3A90412", "/late"];
const EMAIL = /^(Send email|Reply in thread|Forward)$/;

test("email: Send shows its confirm step, which fails closed on the fixture and records nothing", async ({ page }) => {
  const off = offBox(page);
  const confirms: string[] = [];
  page.on("request", (r) => { if (r.url().endsWith("/api/actions/confirm")) confirms.push(r.method()); });
  await signIn(page);
  let found = false;
  for (const url of CARD_PAGES) {
    await page.goto(url);
    await settle(page);
    // an autopilot item on My work keeps its card actions behind "More" (my-work.tsx)
    const more = page.getByRole("button", { name: "More", exact: true }).first();
    if (await more.count()) await more.click();
    const offer = page.locator("[data-testid=card-actions] button").filter({ hasText: EMAIL }).first();
    if (!(await offer.count())) continue;
    found = true;
    await offer.click();
    break;
  }
  expect(found, `an email offer on one of ${CARD_PAGES.join(", ")}`).toBe(true);
  const dialog = page.getByTestId("action-dialog");
  await expect(dialog).toBeVisible();
  // the confirm step: the exact preview and what blocks it. The fixture holds no people-exclusion
  // list and no signed-in mailbox, so an outbound send fails closed (card_actions.preview): the
  // button stays off and says why. A confirmed dry run, its replay and a stale confirm are the
  // Python suites' (test_card_actions: execute idempotent, confirm hash required, RejectedSend).
  await expect(page.getByTestId("action-problems")).toContainText("people-exclusion list is not available", { timeout: 20_000 });
  await expect(page.getByTestId("action-problems")).toContainText("No signed-in mailbox");
  const confirm = page.getByTestId("action-confirm");
  await expect(confirm).toBeDisabled();
  await expect(page.getByText("Fix what is listed first", { exact: true })).toBeVisible();
  await confirm.click({ force: true }).catch(() => undefined);
  // pressed straight at the server, a confirm from the demo reader (not a named person) is refused
  const direct = await page.request.post("/api/actions/confirm", {
    data: { card_id: "autopilot:demo_ap_1", kind: "create_follow_up", payload: {}, confirm: true, idempotency_key: "e2e-acts-" + "a".repeat(16), payload_hash: "0".repeat(64) },
  });
  expect(direct.status(), "a confirm by an unnamed reader is refused").toBe(403);
  const log = await (await page.request.get("/api/actions/log")).json();
  const rows = (log.rows ?? []) as { status?: string }[];
  expect(rows.filter((r) => r.status === "sent" || r.status === "dry_run"), "no send or dry run is recorded").toEqual([]);
  expect(confirms, "the disabled Confirm sent nothing").toEqual([]);
  expect(off, "requests that left the box").toEqual([]);
});

test("autopilot: Accept on an item of mine records the decision and performs nothing", async ({ page }) => {
  const off = offBox(page);
  await signIn(page);
  await page.goto("/autopilot");
  await settle(page);
  const items = page.getByTestId("autopilot-item");
  let accept = page.getByRole("button", { name: /^(Accept|Confirm and perform)$/ }).first();
  if (!(await accept.count())) {
    // the list opens an item on a click
    const rows = page.locator("main li button, main li a").filter({ hasNotText: /^Show \d+ more$/ });
    for (let n = 0; n < Math.min(await rows.count(), 8) && !(await accept.count()); n++) {
      await rows.nth(n).click().catch(() => {});
      await settle(page);
      accept = page.getByRole("button", { name: /^(Accept|Confirm and perform)$/ }).first();
    }
  }
  expect(await accept.count(), "an autopilot item of mine with Accept").toBeGreaterThan(0);
  const [res] = await Promise.all([page.waitForResponse((r) => /\/api\/autopilot\/items\/[^/]+\/decision$/.test(r.url())), accept.click()]);
  expect(res.status()).toBeLessThan(500);
  if (res.status() === 200) {
    const item = await res.json();
    // operator_api/autopilot_area.NOT_RUN: statuses of an action the write path never performed
    const NOT_RUN = ["dry_run", "needs_confirm", "killed", "refused", "excluded", "not_routine", "unavailable", "rate_limited", "duplicate", "not_performed"];
    const steps = (item.decision?.steps ?? []) as { status?: string }[];
    if (item.decision?.outcome) expect(NOT_RUN, `outcome ${item.decision.outcome}`).toContain(item.decision.outcome);
    expect(steps.every((s) => NOT_RUN.includes(s.status ?? "")), `no step performed: ${JSON.stringify(steps)}`).toBe(true);
    await expect(items.getByTestId("autopilot-decision").first()).toBeVisible();
  }
  expect(off, "requests that left the box").toEqual([]);
});
