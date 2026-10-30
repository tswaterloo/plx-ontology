"""An employee's walk through the fixture console in a real browser (Playwright, Chromium).

Starts ``python3 -m operator_ui.tests.case_fixtures --fixture --seed-fixtures --principal
staff:operations --port 0`` on a new state directory, opens the URL it prints in headless
Chromium (``PLAYWRIGHT_BROWSERS_PATH``, preinstalled at /opt/pw-browsers; never installed
here) at 1280 px and 390 px, and asserts what an employee unaided needs: a reload and a
bookmark keep the session without the token in the address bar; home shows the
workbench's proactive gaps above the case list with the number of records scanned; a
case shows its root record's status card first, each blocker cited with a next step; no
text outside an engineer's detail (or a tooltip) shows an internal code; each case header
names its root record and counterparty; a find, a plan and an open record are each an
address that a reload keeps and Back returns from inside the app, a pasted case link opens
the case, a plan's answer renders under its record scrolled into view with its status in
words, and the rail runs the full height at 1280 px; times read in ET; nothing overflows its box and
the page uses one font family. The browser is driven by Node Playwright (the Python
binding is not installed here); the test is skipped, with the reason, only where Node,
its Playwright package or Chromium is absent. Evidence label: isolated-fixture /
injected-transport; credential-shaped variables are removed from both children and the
access URL is passed in the environment, never on a command line or in a message.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from queue import Empty, Queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[2]
CREDENTIAL = re.compile(r"KEY|SECRET|TOKEN|PASSWORD|CREDENTIAL", re.I)
BROWSERS = os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or "/opt/pw-browsers"
NODE_PATHS = [path for path in (os.environ.get("NODE_PATH", "").split(os.pathsep)
                                + ["/opt/node-tools/node_modules"]) if path]

# What an employee must never have to read (objective (3)); an engineer's detail keeps it.
INTERNAL_CODES = (r"po_twin_of|Receipt\.receives|plx\.investigate:|tier_floor|mirror generation"
                  r"|projection built|gold native staging|never authority lineage"
                  r"|\b[\w-]+\.(?:ts|tsx|mjs|py|sql)\b|plx\.documents|\brow\.[a-z]")
# Matched with case: a reason code (PO_LINES_INCOMPLETE) is an engineer's text too.
STRICT_CODES = r"\b[A-Z]{2,}(?:_[A-Z]{2,})+\b"
# A source address is provenance the inspector (#detail) shows; anywhere else it is code.
SOURCE_ADDRESS = r"\b(?:api|graph-mail|sharepoint|bc|original)://"
# The record the find walk opens: the fixture purchase order the seeded cases cite.
FIND_QUERY = "PO5447"

WALK = r"""
const {chromium} = require("playwright");
const URL_WITH_ACCESS = process.env.CONSOLE_URL, CODES = new RegExp(process.env.CODES, "i");
const out = {};
function inspect(scope, codes) {
  // Every text node under ``scope`` (closed <details> included) outside an engineer's detail.
  const problems = {codes: [], engineer: 0, overflow: [], fonts: new Set(), times: [], iso: [], overlap: [], badges: []};
  const root = document.querySelector(scope) || document.body;
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    const parent = node.parentElement, text = node.textContent.trim();
    if (!text || !parent || parent.closest("script, style")) continue;
    if (parent.closest("details.engineer")) { if (new RegExp(codes.loose, "i").test(text)) problems.engineer += 1; continue; }
    if (new RegExp(codes.loose, "i").test(text) || new RegExp(codes.strict).test(text)
        || (new RegExp(codes.source).test(text) && !parent.closest("#detail"))) problems.codes.push(text.slice(0, 160));
    if (/\d{4}-\d{2}-\d{2}T\d{2}:\d{2}|\d{2}:\d{2}:\d{2}\.\d+/.test(text) && !parent.closest("#detail")) problems.iso.push(text.slice(0, 120));
    if (parent.getClientRects().length) problems.fonts.add(getComputedStyle(parent).fontFamily);
  }
  root.querySelectorAll("time").forEach(time => { if (!/^(?:[A-Z][a-z]{2} \d{1,2}, \d{4}, \d{1,2}:\d{2} [AP]M ET|\d{4}-\d{2}-\d{2}|\d{4}-\d{2}-\d{2} \d{2}:\d{2} \(time zone not stated\)|—)$/.test(time.textContent.trim())) problems.times.push(time.textContent); });
  const width = document.documentElement.clientWidth;
  root.querySelectorAll("*").forEach(element => {
    if (!element.getClientRects().length || element.closest("svg")) return;
    const style = getComputedStyle(element), rect = element.getBoundingClientRect();
    const scrolls = /auto|scroll/.test(style.overflowX);
    const inScroller = (() => { for (let up = element.parentElement; up; up = up.parentElement) { if (/auto|scroll/.test(getComputedStyle(up).overflowX) && up !== document.documentElement && up !== document.body) return true; } return false; })();
    const name = `${element.tagName.toLowerCase()}.${String(element.className).trim().replace(/\s+/g, ".")}`;
    if (!scrolls && style.display !== "inline" && element.scrollWidth > element.clientWidth + 1)
      problems.overflow.push(`${name} scroll ${element.scrollWidth} > box ${element.clientWidth}: ${element.textContent.trim().slice(0, 60)}`);
    if (!inScroller && rect.width && rect.right > width + 1) problems.overflow.push(`${name} ends at ${Math.round(rect.right)} past ${width}: ${element.textContent.trim().slice(0, 60)}`);
  });
  root.querySelectorAll(".field").forEach(field => {
    const label = field.querySelector(".field-label"), value = field.querySelector(".field-value");
    if (!label || !value) return;
    const a = label.getBoundingClientRect(), b = value.getBoundingClientRect();
    const overlaps = a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom;
    if (overlaps) problems.overlap.push(label.textContent);
  });
  root.querySelectorAll(".tag").forEach(tag => {
    const range = document.createRange(); range.selectNodeContents(tag);
    const text = range.getBoundingClientRect().width, box = tag.getBoundingClientRect().width;
    if (box > text + 40) problems.badges.push(`${tag.textContent} ${Math.round(box)}px for ${Math.round(text)}px of text`);
  });
  if (document.documentElement.scrollWidth > width + 1) problems.overflow.push(`page ${document.documentElement.scrollWidth} > ${width}`);
  problems.fonts = [...problems.fonts];
  return problems;
}
async function check(page, scope, label, store) {
  store[label] = await page.evaluate(([fn, s, c]) => eval(`(${fn})`)(s, c), [inspect.toString(), scope, {loose: process.env.CODES, strict: process.env.STRICT_CODES, source: process.env.SOURCE_ADDRESS}]);
}
(async () => {
  const browser = await chromium.launch({headless: true});
  for (const width of [1280, 390]) {
    const result = out[width] = {};
    const context = await browser.newContext({viewport: {width, height: 900}});
    const page = await context.newPage();
    await page.goto(URL_WITH_ACCESS);
    await page.waitForFunction(() => document.getElementById("session-role").textContent.trim() && document.querySelector("[data-home=proactive]"));
    result.address = await page.evaluate(() => ({access: location.href.includes("access="), hash: location.hash}));
    result.top = await page.evaluate(() => [document.getElementById("session-status").textContent, document.getElementById("session-role").textContent]);
    await page.reload();
    await page.waitForFunction(() => document.querySelector("[data-home=proactive]") || document.getElementById("notice").textContent.trim(), null, {timeout: 60000});
    result.reload = await page.evaluate(() => ({home: Boolean(document.querySelector("[data-home=proactive]")), notice: document.getElementById("notice").textContent}));
    const home = await page.evaluate(() => {
      const found = document.querySelector("[data-home=proactive]"), cases = document.querySelector("[data-home=cases]");
      return {above: Boolean(found && cases && found.getBoundingClientRect().top < cases.getBoundingClientRect().top),
        scanned: found ? (found.querySelector(".home-scanned") || {}).textContent || "" : "", text: found ? found.innerText : ""};
    });
    result.homePanel = home;
    await check(page, "body", "homeChecks", result);
    // A bookmark: the address without the token, opened in a new tab beside a signed-in one.
    const bookmark = await context.newPage();
    await bookmark.goto(page.url().split("#")[0] + "#view=cases");
    await bookmark.waitForFunction(() => document.querySelector(".list-row") || document.getElementById("notice").textContent.trim(), null, {timeout: 60000});
    result.bookmark = await bookmark.evaluate(() => ({rows: document.querySelectorAll(".list-row").length, notice: document.getElementById("notice").textContent}));
    await bookmark.close();
    // Find and open: each step is a history entry, a reload keeps it and Back stays in the app.
    const origin = new URL(page.url()).origin;
    const find = result.find = {plans: []};
    await page.click("[data-view=objects]");
    await page.fill(".ow-search input", process.env.FIND_QUERY);
    await page.click(".ow-search button");
    await page.waitForSelector(".ow-candidate", {timeout: 60000});
    find.searchHash = await page.evaluate(() => location.hash);
    await check(page, "#view", "findChecks", find);
    const planCount = await page.$$eval(".ow-plan", plans => plans.length);
    for (let index = 0; index < (width === 1280 ? planCount : Math.min(planCount, 2)); index += 1) {
      await page.evaluate(() => window.scrollTo(0, 0));
      await (await page.$$(".ow-plan"))[index].click();
      await page.waitForSelector(".ow-answer-slot .ow-investigation", {timeout: 60000});
      await page.waitForTimeout(600);
      const plan = await page.evaluate(() => {
        const slot = document.querySelector(".ow-answer-slot"), rect = slot.getBoundingClientRect();
        const status = slot.querySelector(".ow-answerability");
        return {hash: location.hash, family: slot.getAttribute("data-family"), inRow: Boolean(slot.closest(".ow-candidate-row")),
          top: Math.round(rect.top), height: innerHeight, slots: document.querySelectorAll(".ow-answer-slot").length,
          status: status ? status.textContent : ""};
      });
      await check(page, "#view", "checks", plan);
      find.plans.push(plan);
    }
    await page.evaluate(() => window.scrollTo(0, 0));
    await page.click(".ow-candidate");
    await page.waitForSelector("[data-section=card]", {timeout: 60000});
    find.objectHash = await page.evaluate(() => location.hash);
    // The section jumps scroll within the page: no fragment is written.
    const jumps = await page.$$(".ow-jump-button");
    find.jump = {present: jumps.length > 1, before: find.objectHash};
    if (jumps.length > 1) { await jumps[jumps.length - 1].click(); await page.waitForTimeout(300); }
    find.jump.after = await page.evaluate(() => location.hash);
    await page.evaluate(() => window.scrollTo(0, 0));
    find.objectTitle = await page.evaluate(() => document.querySelector("[data-section=card] h2, [data-section=card] h3").textContent);
    await check(page, "#view", "objectChecks", find);
    await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight));
    await page.waitForTimeout(200);
    find.rail = await page.evaluate(() => { const r = document.querySelector(".rail").getBoundingClientRect(); return {top: Math.round(r.top), bottom: Math.round(r.bottom), height: innerHeight, stacked: getComputedStyle(document.querySelector(".app-shell")).display === "block"}; });
    await page.reload();
    await page.waitForSelector("[data-section=card]", {timeout: 60000});
    find.reload = await page.evaluate(() => ({hash: location.hash, title: document.querySelector("[data-section=card] h2, [data-section=card] h3").textContent, notice: document.getElementById("notice").textContent}));
    await page.goBack();
    await page.waitForSelector(".ow-answer-slot .ow-investigation", {timeout: 60000});
    find.back = await page.evaluate(() => ({origin: location.origin, hash: location.hash, card: Boolean(document.querySelector("[data-section=card]")), candidates: document.querySelectorAll(".ow-candidate").length}));
    find.back.sameOrigin = find.back.origin === origin; delete find.back.origin;
    await page.click("[data-view=cases]");
    await page.waitForSelector(".list-row");
    const count = await page.$$eval(".list-row", rows => rows.length);
    result.cases = [];
    for (let index = 0; index < (width === 1280 ? count : Math.min(count, 2)); index += 1) {
      const back = await page.$("text=All cases");
      if (back) await back.click();
      await page.waitForSelector(".list-row");
      await (await page.$$(".list-row"))[index].click();
      await page.waitForSelector(".case-header .case-facts", {timeout: 60000});
      const header = await page.evaluate(() => {
        const head = document.querySelector(".case-header");
        const facts = [...head.querySelectorAll(".case-facts dt")].map(dt => [dt.textContent, dt.nextElementSibling ? dt.nextElementSibling.textContent : ""]);
        return {title: head.querySelector("h2").textContent, facts, route: location.hash};
      });
      // The root's status card: the case's first element, each blocker cited with a next step.
      const card = await page.evaluate(() => {
        const first = document.querySelector(".case-workspace").firstElementChild;
        const cards = [...document.querySelectorAll(".case-workspace .status-card")];
        return {first: first ? first.className : "", count: cards.length,
          headline: cards.map(card => (card.querySelector(".ow-sc-headline") || card).textContent),
          blockers: cards.map(card => [...card.querySelectorAll(".ow-sc-blocker")].map(b => ({
            title: b.querySelector("strong").textContent, next: b.querySelector(".ow-sc-next").textContent,
            cites: b.querySelectorAll(".case-chip").length})))};
      });
      const item = {header, card};
      await check(page, "body", "page", item);
      result.cases.push(item);
      if (index === 0) {
        await page.reload();
        await page.waitForSelector(".case-header .case-facts", {timeout: 60000});
        result.caseReload = await page.evaluate(() => document.querySelector(".case-header h2").textContent);
        await (await page.$(".case-chip")).click();
        await page.waitForSelector("#detail:not(.hidden) .field", {timeout: 60000});
        await check(page, "#detail", "inspector", result);
        await page.click("#close-detail");
        // Back from a case returns to the list inside the app; a pasted case link opens the case.
        await page.goBack();
        await page.waitForSelector(".list-row", {timeout: 60000});
        result.caseBack = await page.evaluate(([o]) => ({sameOrigin: location.origin === o, hash: location.hash, rows: document.querySelectorAll(".list-row").length}), [origin]);
        const caseId = decodeURIComponent(header.route.split("case=")[1].split("&")[0]);
        await page.evaluate(id => { location.hash = "#case=" + encodeURIComponent(id); }, caseId);
        await page.waitForFunction(id => document.querySelector(".case-header h2") && location.hash.includes("case=" + encodeURIComponent(id)), caseId, {timeout: 60000});
        result.pasted = await page.evaluate(() => ({title: document.querySelector(".case-header h2").textContent, hash: location.hash}));
        result.pastedWant = {title: header.title, hash: header.route};
      }
    }
    await context.close();
  }
  await browser.close();
  process.stdout.write(JSON.stringify(out));
})().catch(error => { process.stderr.write(String(error && error.stack || error)); process.exit(1); });
"""


def _skip_reason() -> str | None:
    node = shutil.which("node")
    if node is None:
        return "Node.js is not installed: the walk-through drives Chromium with Node Playwright"
    probe = subprocess.run([node, "-e", "require.resolve('playwright')"], capture_output=True,
                           env={**_clean_env(), "NODE_PATH": os.pathsep.join(NODE_PATHS)})
    if probe.returncode != 0:
        return "the Node playwright package is not installed (never installed by this test)"
    if not any(Path(BROWSERS).glob("chromium*")):
        return f"no Chromium under {BROWSERS} (PLAYWRIGHT_BROWSERS_PATH); never installed here"
    return None


def _clean_env() -> dict[str, str]:
    return {name: value for name, value in os.environ.items() if not CREDENTIAL.search(name)}


def walk_console() -> dict:
    """Start the fixture console on a fresh state directory, walk it in Chromium and return
    the walk's JSON; the console is stopped on every path. The access token never leaves."""
    with tempfile.TemporaryDirectory(prefix="console-walkthrough-") as temp:
        env = {**_clean_env(), "PYTHONPATH": str(ROOT)}
        env.pop("PYTHONUNBUFFERED", None)
        proc = subprocess.Popen(
            [sys.executable, "-m", "operator_ui.tests.case_fixtures", "--state-dir",
             str(Path(temp) / "state"), "--fixture", "--seed-fixtures",
             "--principal", "staff:operations", "--port", "0"],
            cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        try:
            lines: Queue = Queue()
            threading.Thread(target=lambda: [lines.put(line) for line in proc.stdout],
                             daemon=True).start()
            url = None
            while url is None:
                try:
                    line = lines.get(timeout=300)
                except Empty:
                    raise AssertionError("the fixture console printed no URL") from None
                found = re.search(r"Operator console: (http://127\.0\.0\.1:\d+/#access=\S+)", line)
                if found:
                    url = found[1]
                elif proc.poll() is not None and lines.empty():
                    raise AssertionError("the fixture console exited before serving")
            script = Path(temp) / "walk.js"
            script.write_text(WALK, encoding="utf-8")
            walked = subprocess.run(
                ["node", str(script)], capture_output=True, text=True, timeout=900,
                env={**_clean_env(), "NODE_PATH": os.pathsep.join(NODE_PATHS),
                     "PLAYWRIGHT_BROWSERS_PATH": BROWSERS, "CONSOLE_URL": url,
                     "CODES": INTERNAL_CODES, "STRICT_CODES": STRICT_CODES,
                     "SOURCE_ADDRESS": SOURCE_ADDRESS, "FIND_QUERY": FIND_QUERY})
            token = url.split("#access=", 1)[1]
            if walked.returncode != 0:
                raise AssertionError("the walk-through failed: "
                                     + walked.stderr.replace(token, "<token>")[-3000:])
            return json.loads(walked.stdout)
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=20)
            proc.stdout.close()


def _pages(walk: dict) -> list:
    find = walk["find"]
    return ([("home", walk["homeChecks"]), ("inspector", walk["inspector"]),
             ("find", find["findChecks"]), ("object", find["objectChecks"])]
            + [("plan " + plan["family"], plan["checks"]) for plan in find["plans"]]
            + [(case["header"]["title"], case["page"]) for case in walk["cases"]])


PLAN_STATUS = r"Plan status: (answered|partly answered|cannot be answered|status unknown)\b"
STEPS = {
    "session_address": lambda w: (not w["address"]["access"] and w["address"]["hash"] == "#view=home"
                                  and w["reload"] == {"home": True, "notice": ""}),
    "bookmark": lambda w: w["bookmark"]["notice"] == "" and w["bookmark"]["rows"] > 0,
    "find_route": lambda w: w["find"]["searchHash"] == "#view=objects&q=" + FIND_QUERY,
    "plan_answer_scrolled": lambda w: len(w["find"]["plans"]) > 1 and all(
        p["inRow"] and p["slots"] == 1 and "&family=" + p["family"] + "&root=" in p["hash"]
        and 0 <= p["top"] < p["height"] // 3 and re.search(PLAN_STATUS, p["status"])
        for p in w["find"]["plans"]),
    "jump_no_fragment": lambda w: w["find"]["jump"]["present"] and w["find"]["jump"]["after"] == w["find"]["jump"]["before"],
    "open_record_route": lambda w: w["find"]["objectHash"].startswith(w["find"]["searchHash"] + "&object="),
    "reload_keeps_record": lambda w: w["find"]["reload"] == {"hash": w["find"]["objectHash"], "title": w["find"]["objectTitle"], "notice": ""},
    "back_stays_in_app": lambda w: (w["find"]["back"]["sameOrigin"] and not w["find"]["back"]["card"]
                                    and w["find"]["back"]["hash"] == w["find"]["plans"][-1]["hash"]
                                    and w["caseBack"]["sameOrigin"] and w["caseBack"]["hash"] == "#view=cases"),
    "case_reload": lambda w: w["caseReload"] == w["cases"][0]["header"]["title"],
    "pasted_case_link": lambda w: w["pasted"] == w["pastedWant"],
    "employee_text": lambda w: all(not c["codes"] and not c["iso"] and not c["times"] for _, c in _pages(w)),
    "layout": lambda w: all(not c["overflow"] and not c["overlap"] and not c["badges"] and len(c["fonts"]) == 1 for _, c in _pages(w)),
}


def step_results(walk: dict) -> dict:
    """Pass/fail per step over both widths; a step whose data is missing fails closed."""
    steps = {}
    for name, rule in STEPS.items():
        widths = {}
        for width in ("1280", "390"):
            try:
                widths[width] = "pass" if rule(walk[width]) else "fail"
            except (KeyError, IndexError, TypeError):
                widths[width] = "fail"
        steps[name] = {"status": "pass" if set(widths.values()) == {"pass"} else "fail", "widths": widths}
    try:
        rail = walk["1280"]["find"]["rail"]
        ok = not rail["stacked"] and rail["top"] == 0 and rail["bottom"] >= rail["height"]
    except (KeyError, TypeError):
        ok = False
    steps["rail_full_height"] = {"status": "pass" if ok else "fail", "widths": {"1280": "pass" if ok else "fail"}}
    passed = sum(step["status"] == "pass" for step in steps.values())
    return {"walk_completed": True, "steps": steps, "steps_total": len(steps),
            "steps_passed": passed, "steps_failed": len(steps) - passed}


SKIP = _skip_reason()


@unittest.skipIf(SKIP is not None, f"browser walk-through skipped: {SKIP}")
class ConsoleWalkthroughTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.walk = walk_console()

    def widths(self):
        for width in ("1280", "390"):
            with self.subTest(width=width):
                yield self.walk[width]

    def test_reload_and_bookmark_keep_the_session_without_the_token_in_the_address(self):
        for walk in self.widths():
            self.assertFalse(walk["address"]["access"], walk["address"])
            self.assertEqual(walk["address"]["hash"], "#view=home")
            self.assertEqual(walk["top"], ["Fixture session · not a signed-in employee",
                                           "Role: Operations staff"])
            self.assertEqual(walk["reload"], {"home": True, "notice": ""})
            self.assertEqual(walk["bookmark"]["notice"], "")
            self.assertGreater(walk["bookmark"]["rows"], 0)
            self.assertEqual(walk["caseReload"], walk["cases"][0]["header"]["title"])

    def test_find_open_reload_and_back_keep_the_record_in_the_app(self):
        for walk in self.widths():
            find = walk["find"]
            self.assertEqual(find["searchHash"], "#view=objects&q=" + FIND_QUERY)
            self.assertTrue(find["objectHash"].startswith(find["searchHash"] + "&object="), find["objectHash"])
            self.assertNotIn("family=", find["objectHash"])
            self.assertEqual(find["reload"], {"hash": find["objectHash"], "title": find["objectTitle"], "notice": ""})
            self.assertTrue(find["back"]["sameOrigin"])
            self.assertFalse(find["back"]["card"], "Back leaves the record for the plan answer before it")
            self.assertGreater(find["back"]["candidates"], 0)
            self.assertEqual(find["back"]["hash"], find["plans"][-1]["hash"])
            # The jump bar scrolls within the page: no fragment is written.
            self.assertTrue(find["jump"]["present"])
            self.assertEqual(find["jump"]["after"], find["jump"]["before"])
            self.assertEqual(walk["caseBack"]["hash"], "#view=cases")
            self.assertTrue(walk["caseBack"]["sameOrigin"])
            self.assertGreater(walk["caseBack"]["rows"], 0)
            self.assertEqual(walk["pasted"], walk["pastedWant"])

    def test_a_plan_answers_under_its_record_scrolled_into_view_with_its_status_in_words(self):
        for walk in self.widths():
            plans = walk["find"]["plans"]
            self.assertGreater(len(plans), 1)
            for plan in plans:
                with self.subTest(family=plan["family"]):
                    self.assertTrue(plan["inRow"], plan)
                    self.assertEqual(plan["slots"], 1, "one answer at a time")
                    self.assertIn("&family=" + plan["family"] + "&root=", plan["hash"])
                    self.assertGreaterEqual(plan["top"], 0, plan)
                    self.assertLess(plan["top"], plan["height"] // 3, "the answer is scrolled to")
                    self.assertRegex(plan["status"], r"Plan status: (answered|partly answered|cannot be answered|status unknown)\b")

    def test_the_rail_is_full_height_beside_a_long_page(self):
        rail = self.walk["1280"]["find"]["rail"]
        self.assertFalse(rail["stacked"])
        self.assertEqual(rail["top"], 0, rail)
        self.assertGreaterEqual(rail["bottom"], rail["height"], rail)
        self.assertTrue(self.walk["390"]["find"]["rail"]["stacked"], "at 390 px the rail stacks above the page")

    def test_home_puts_the_proactive_scan_first_with_its_scanned_count(self):
        for walk in self.widths():
            self.assertTrue(walk["homePanel"]["above"])
            scanned = re.fullmatch(r"Scanned (\d+) records? you may read.*", walk["homePanel"]["scanned"])
            self.assertIsNotNone(scanned, walk["homePanel"]["scanned"])
            if scanned[1] == "0":
                # Nothing read is not a clean result.
                self.assertNotIn("No gaps found", walk["homePanel"]["text"])
                self.assertIn("not a clean result", walk["homePanel"]["text"])

    def test_case_headers_name_the_root_record_and_counterparty(self):
        for walk in self.widths():
            for case in walk["cases"]:
                header = case["header"]
                facts = dict(header["facts"])
                self.assertNotRegex(header["title"], r"case-|;", header)
                self.assertTrue(any(name in facts for name in ("Purchase order", "Assembly order",
                                                               "Sales order")), header)
                party = next((value for name, value in header["facts"]
                              if name in ("Supplier", "Customer", "Vendor", "Counterparty")), None)
                self.assertTrue(party, header)
                self.assertRegex(header["route"], r"^#view=cases&case=case-")

    def test_a_case_shows_its_root_status_card_first_with_cited_blockers_and_next_steps(self):
        for walk in self.widths():
            for case in walk["cases"]:
                card = case["card"]
                with self.subTest(case=case["header"]["title"]):
                    facts = dict(case["header"]["facts"])
                    if "Purchase order" in facts:   # a typed root this reader reads: its card
                        self.assertEqual(card["count"], 1, card)
                        self.assertNotRegex(card["headline"][0], r"^No status card", card)
                    if card["count"]:
                        self.assertEqual(card["first"], "case-status-cards", card)
                    for blockers in card["blockers"]:
                        self.assertLessEqual(len(blockers), 5)
                        for blocker in blockers:
                            self.assertGreater(blocker["cites"], 0, blocker)
                            self.assertRegex(blocker["next"], r"^Next: [A-Z][^:]+: \S", blocker)
            self.assertTrue(any(case["card"]["count"] for case in walk["cases"]), walk["cases"])

    def test_no_internal_code_raw_timestamp_overflow_or_second_font(self):
        for walk in self.widths():
            find = walk["find"]
            pages = [("home", walk["homeChecks"]), ("inspector", walk["inspector"]),
                     ("find", find["findChecks"]), ("object", find["objectChecks"])]
            pages += [("plan " + plan["family"], plan["checks"]) for plan in find["plans"]]
            pages += [(case["header"]["title"], case["page"]) for case in walk["cases"]]
            # The codes are there for an engineer, so the probe above has something to miss.
            self.assertGreater(sum(checks["engineer"] for _, checks in pages), 0)
            for name, checks in pages:
                with self.subTest(page=name):
                    self.assertEqual(checks["codes"], [], "internal code shown to an employee")
                    self.assertEqual(checks["iso"], [], "a raw timestamp outside the inspector")
                    self.assertEqual(checks["times"], [], "a time not in ET")
                    self.assertEqual(checks["overflow"], [], "an element overflows its box")
                    self.assertEqual(checks["overlap"], [], "a field name overlaps its value")
                    self.assertEqual(checks["badges"], [], "a badge wider than its text")
                    self.assertEqual(len(checks["fonts"]), 1, checks["fonts"])

    def test_live_check_reports_every_step_passing_on_the_real_walk(self):
        from verify import live_verify
        summary = {"schema": "plx-console-walkthrough/v1", "evidence": "isolated-fixture",
                   "blocked_reason": None, **step_results(self.walk)}
        failed = [name for name, step in summary["steps"].items() if step["status"] != "pass"]
        self.assertEqual(failed, [])
        spec = json.loads((ROOT / "devloop/live-checks/workspace.console_walkthrough.json").read_text())
        live_verify.validate_spec(spec, stem="workspace.console_walkthrough")
        self.assertEqual(live_verify.check_expectations(spec, 0, summary), [])
        broken = json.loads(json.dumps(self.walk))
        del broken["390"]["find"]["reload"]
        self.assertEqual(step_results(broken)["steps"]["reload_keeps_record"]["status"], "fail")


if __name__ == "__main__":
    unittest.main()
