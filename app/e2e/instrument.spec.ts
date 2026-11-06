import { expect, test } from "@playwright/test";

// The instrument views against operator_api --mock (fixture data): the command bar finds a
// record from the keyboard, a purchase order draws its chain of custody (a chart from 560 px,
// a spine below), Today draws the late list, and an answer's claims carry numbered citations
// that its sources list names. Nothing here sends, confirms or approves anything.

test.beforeEach(async ({ page }) => {
  await page.goto("/signin");
  await page.getByRole("button", { name: "Continue with demo sign-in" }).click();
  await expect(page).not.toHaveURL(/\/signin/);
});

test("the command bar finds a supplier from the keyboard and opens it", async ({ page }, info) => {
  test.skip(info.project.name !== "desktop", "a keyboard shortcut");
  await page.goto("/late");
  await page.keyboard.press("Control+k");
  const palette = page.getByTestId("command-palette");
  await expect(palette).toBeVisible();
  await page.getByTestId("palette-input").fill("maple");
  const record = palette.getByRole("option", { name: /Maple Leaf Soapworks/ });
  await expect(record).toBeVisible();
  // "Ask maple" is first; one step down is the record
  await expect(palette.getByRole("option").first()).toContainText("Ask maple");
  await page.keyboard.press("ArrowDown");
  await expect(record).toHaveAttribute("aria-selected", "true");
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(/\/object\/Supplier\//);
  await expect(page.getByTestId("object-title")).toContainText("Maple Leaf Soapworks");
});

test("a purchase order draws its chain of custody", async ({ page }, info) => {
  await page.goto("/po/90412");
  const custody = page.getByTestId("custody");
  await expect(custody).toBeVisible();
  await expect(custody).toContainText("needed by 17 Oct");
  await expect(custody).toContainText("12 days late");
  if (info.project.name === "desktop") {
    const marks = custody.getByTestId("custody-mark");
    expect(await marks.count()).toBeGreaterThanOrEqual(4);
    // the readout names the focused event; the keyboard moves it
    await custody.getByTestId("custody-chart").focus();
    await page.keyboard.press("Home");
    await expect(custody.getByTestId("custody-readout")).toContainText("PO");
  } else {
    await expect(custody.getByTestId("custody-spine")).toBeVisible();
  }
});

test("Late POs draws the late list; an answer's claims cite numbered sources", async ({ page }) => {
  // the drawing of every late PO sits on Late POs (Today lists them)
  await page.goto("/late");
  await expect(page.getByTestId("late-shape")).toBeVisible();
  expect(await page.getByTestId("late-dot").count()).toBe(2);
  await expect(page.getByTestId("late-readout")).toContainText("days late");

  await page.goto("/ask?q=" + encodeURIComponent("why is the soap order from that canadian guy late"));
  const card = page.getByTestId("answer-card").last();
  await expect(card).toHaveAttribute("aria-busy", "false", { timeout: 20_000 });
  const sources = card.getByTestId("answer-source");
  expect(await sources.count()).toBeGreaterThanOrEqual(3);
  // every marker's number is a row of the list
  const numbers = await card.getByTestId("source-chip").allTextContents();
  const rows = await sources.count();
  for (const n of numbers.map((t) => t.trim()).filter((t) => /^\d+$/.test(t))) expect(Number(n)).toBeLessThanOrEqual(rows);
});
