// Drives the isolated fixture console in headless Chromium and saves the flagship-walkthrough
// screenshots. Started by scripts/eval_flagship_screenshots.py (which passes CONSOLE_URL and OUT_DIR
// in the environment; the access URL is never on a command line). Synthetic data only.
// The fixture console serves no Ask planner (--ask needs a headless model), so the Ask step
// screenshots the honest state: the question list and no Ask box. No answer is faked.
const {chromium} = require("playwright");
const path = require("path");
const OUT = process.env.OUT_DIR, URL_WITH_ACCESS = process.env.CONSOLE_URL;
const EXPLORE = process.env.EXPLORE === "1";
const ORDER = "PO5447"; // the fixture's synthetic purchase order
const file = name => path.join(OUT, name);
const say = async (page, label, sel) => {
  if (EXPLORE) console.log(`=== ${label}\n` + (await page.$$eval(sel, els => els.map(e => e.innerText))).join("\n"));
};

(async () => {
  const browser = await chromium.launch({headless: true});
  const context = await browser.newContext({viewport: {width: 1280, height: 760}});
  const page = await context.newPage();
  await page.goto(URL_WITH_ACCESS);
  await page.waitForSelector("[data-home=proactive]", {timeout: 60000});
  await page.waitForSelector(".list-row");
  // 1. Home: the proactive scan above the case list (viewport only, as an employee lands).
  await page.screenshot({path: file("01-home.png")});

  // 2. Find the purchase order by its number, then open it.
  await page.click("[data-view=objects]");
  await page.waitForSelector("#view input[type=search]");
  await page.fill("#view input[type=search]", ORDER);
  await page.click(".ow-search button");
  await page.waitForSelector(`#view button:has-text('${ORDER}')`, {timeout: 60000});
  await page.screenshot({path: file("02-find.png")});
  await page.click(`#view button:has-text('${ORDER}')`);
  await page.waitForSelector(".ow-view-slot .ow-workspace", {timeout: 60000});
  // 3. Ask: the fixture serves no planner, so the order shows no Ask box. The question
  // families it does offer are the crop below; the doc says so plainly. Nothing is faked.
  if (EXPLORE) console.log("ask box present: " + Boolean(await page.$(".ow-ask")));
  await (await page.$(".ow-results-slot")).screenshot({path: file("03-ask-not-served.png")});

  // 4. Landed cost of the order, with its named missing inputs.
  await page.click("#view button:has-text('Landed cost of a purchase order')");
  await page.waitForSelector(".ow-view-slot .ow-record-case", {timeout: 120000});
  await page.waitForTimeout(500);
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.$eval(".ow-view-slot", el => el.scrollIntoView({block: "start"}));
  await page.screenshot({path: file("04-landed-cost.png")});
  const missing = page.locator(".ow-view-slot").getByText("Not established: po lines incomplete").first();
  if (await missing.count()) {
    await missing.scrollIntoViewIfNeeded();
    await page.screenshot({path: file("05-landed-cost-missing.png")});
  }

  // 5. Record the answer on a case: follow-ups and drafts, nothing sent.
  await page.click(".ow-record-case button:has-text('Record on case')");
  await page.waitForFunction(() => /Nothing was sent|Not recorded/.test(document.querySelector(".ow-record-case .ow-review-status").textContent), null, {timeout: 120000});
  await say(page, "RECORDED", ".ow-record-case");
  const record = await page.$(".ow-record-case");
  await record.scrollIntoViewIfNeeded();
  await page.screenshot({path: file("06-follow-up-recorded.png")});

  // A follow-up proposal: a person approves it; nothing is executed.
  const form = page.locator(".ow-view-slot .ow-follow-up:not(.ow-record-case)").first();
  await form.getByLabel("What is missing").fill("The receipt's freight and duty actuals for this order");
  await form.getByLabel("Why it matters").fill("Landed cost stays unknown until the warehouse confirms what was received");
  await form.getByLabel("Who can answer").fill("warehouse, purchasing");
  await form.getByRole("button", {name: "Propose"}).click();
  await page.waitForFunction(() => /Nothing was executed|Not proposed/.test(
    document.querySelector(".ow-view-slot .ow-follow-up:not(.ow-record-case) .ow-review-status").textContent), null, {timeout: 120000});
  await say(page, "PROPOSED", ".ow-view-slot .ow-follow-up:not(.ow-record-case)");
  await form.scrollIntoViewIfNeeded();
  await page.screenshot({path: file("07-proposal-not-executed.png")});

  // Mobile: home at phone width.
  const phone = await browser.newContext({viewport: {width: 390, height: 844}});
  const small = await phone.newPage();
  await small.goto(URL_WITH_ACCESS);
  await small.waitForSelector("[data-home=proactive]", {timeout: 60000});
  await small.screenshot({path: file("08-home-phone.png")});
  await browser.close();
})().catch(error => { process.stderr.write(String(error && error.stack || error)); process.exit(1); });
