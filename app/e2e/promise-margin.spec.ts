import { expect, test, type Page } from "@playwright/test";
import path from "node:path";

// "ETA confirmed?" on purchase orders and the late list, and the margin section on order
// and customer pages, against operator_api --mock (fixture data, isolated-fixture).
// Screenshots at desktop and 390 px into e2e/screenshots.

const shots = path.join(__dirname, "screenshots");

async function shot(page: Page, name: string, project: string) {
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  expect(overflow, `${name} overflows horizontally by ${overflow}px`).toBeLessThanOrEqual(0);
  await page.waitForTimeout(700); // the page fades in
  await page.screenshot({ path: path.join(shots, `${project}-${name}.png`), fullPage: true });
}

test("ETA confirmed and margin why", async ({ page }, info) => {
  test.setTimeout(150_000);
  const project = info.project.name;
  await page.goto("/signin");
  await page.getByRole("button", { name: "Continue with demo sign-in" }).click();
  await expect(page).not.toHaveURL(/\/signin/);

  // A firm, per-line confirmation: each line its own date; one line past its promise.
  await page.goto("/po/90412");
  const eta = page.getByTestId("eta-card");
  await expect(eta.getByTestId("eta-badge")).toHaveText("Confirmed by supplier, per line");
  await expect(eta.getByTestId("eta-late")).toHaveText("Late vs supplier's promise by 5 days");
  await expect(eta.getByTestId("eta-line")).toHaveCount(2);
  await expect(eta.getByTestId("eta-line").first()).toContainText("Late by 5 days");
  await expect(eta.getByTestId("eta-line").first()).toContainText("matched by item code");
  await shot(page, "eta-per-line", project);
  await eta.getByRole("button", { name: "Open confirmation" }).click();
  await expect(page.getByRole("dialog")).toBeVisible();
  await page.keyboard.press("Escape");

  // A lead: its date is stated, never counted.
  await page.goto("/po/90455");
  await expect(page.getByTestId("eta-badge")).toHaveText("Possible confirmation (unconfirmed link)");
  await shot(page, "eta-possible", project);

  // None found.
  await page.goto("/po/90388");
  await expect(page.getByTestId("eta-badge")).toHaveText("No supplier confirmation found");
  await shot(page, "eta-none", project);

  // The late list says "none found" only when no confirmation is linked at all.
  await page.goto("/late");
  await expect(page.getByTestId("late-eta").first()).toHaveText("No supplier confirmation found");
  await shot(page, "late-list-eta", project);

  // An order's margin, its verdict and the why in one or two sentences, with sources.
  await page.goto("/object/SalesOrder/SalesOrder%3Ademo%3A77120");
  const margin = page.getByTestId("margin");
  await expect(margin.getByTestId("margin-verdict").first()).toHaveText("Profitable");
  await expect(margin.getByTestId("margin-why")).toContainText("Item cost rose 36.5% (1.59 → 2.17 CAD/PCS) while the price was unchanged");
  await expect(margin.getByTestId("margin-sources")).toBeVisible();
  await shot(page, "margin-order", project);

  // No valid cost in BC.
  await page.goto("/object/SalesOrder/SalesOrder%3Ademo%3A77131");
  await expect(page.getByTestId("margin-verdict")).toHaveText("No valid cost in BC");
  await shot(page, "margin-no-cost", project);

  // A customer: verdict, the worst order's why.
  await page.goto("/customer/glow");
  await expect(page.getByTestId("customer-verdict")).toBeVisible();
  await shot(page, "margin-customer", project);

  // A customer whose figures are Sage's: before credit notes, no margin.
  await page.goto("/customer/harbor");
  await expect(page.getByTestId("margin-verdict")).toHaveText("Sage, before credit notes");
  await shot(page, "margin-sage", project);
});
