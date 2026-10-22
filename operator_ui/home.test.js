"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

class Element {
  constructor(tagName) {
    this.tagName = tagName;
    this.children = [];
    this.textContent = "";
    this.className = "";
    this.dataset = {};
    this.listeners = {};
    this.classList = {contains: () => false, toggle: () => {}, remove: () => {}, add: () => {}};
  }
  appendChild(child) { this.children.push(child); return child; }
  removeChild(child) { this.children.splice(this.children.indexOf(child), 1); return child; }
  addEventListener(event, handler) { this.listeners[event] = handler; }
  setAttribute() {}
  get firstChild() { return this.children[0] || null; }
}

function walk(node) { return [node, ...node.children.flatMap(walk)]; }
function load(fetchImpl) {
  const ids = {};
  const document = {createElement: tag => new Element(tag),
    getElementById: id => (ids[id] = ids[id] || new Element("div")),
    querySelectorAll: () => [], addEventListener: () => {}};
  const context = vm.createContext({document, URL, URLSearchParams, fetch: fetchImpl, setTimeout});
  vm.runInContext(fs.readFileSync(path.join(__dirname, "app.js"), "utf8"), context);
  return {context, ids};
}

const H = "api://public.fixture_rows/%7B%22id%22%3A1%7D@g1-1";
const HOME = {
  cases: {status: "read", open_cases: 1, open_gaps: 1, cases: [{case_id: "case-1", question: "Is SYNTH-1 late?", gaps: [
    {id: "G1", state: "unknown", missing: "Why late", why: "Nothing recorded.", citations: [{handle: H, status: "resolved"}]}]}]},
  proactive: {status: "read", scanned: 4, gaps: [{id: "pg-1", kind: "missing_in_record", summary: "PO number is held empty.",
    citations: [H], follow_up: {object_id: "Order:1", missing: "PO number", why: "w", who_can_answer: ["sales"], state: "unknown", priority: "medium"}}]}};

test("home lists the open cases with their gaps and the gaps found by rule, each cited", () => {
  const {context} = load();
  const rows = context.homeFoundRows(HOME.proactive);
  assert.deepEqual(JSON.parse(JSON.stringify(rows.map(r => [r.id, r.kind, r.cites.map(c => c.handle)]))),
    [["pg-1", "Missing in the record", [H]]]);
  const state = vm.runInContext("state", context);
  state.home = HOME;
  const view = new Element("section");
  context.homeView(view);
  const shown = walk(view).map(node => node.textContent).join(" ");
  for (const word of ["Is SYNTH-1 late?", "Why late", "PO number is held empty.", "Propose follow-up"])
    assert.ok(shown.includes(word), word);
  assert.equal(walk(view).filter(node => node.className === "case-chip").length, 2);
});

test("proposing a follow-up posts the detected gap's own follow-up and nothing else", async () => {
  const calls = [];
  const {context} = load(async (url, options) => { calls.push([url, options]); return {ok: true, json: async () => ({}) }; });
  const state = vm.runInContext("state", context);
  state.home = HOME;
  state.token = "t";
  const view = new Element("section");
  context.homeView(view);
  const button = walk(view).find(node => node.textContent === "Propose follow-up");
  await button.listeners.click();
  assert.equal(calls.length, 1);
  assert.equal(calls[0][0], "/api/object-follow-up");
  assert.deepEqual(JSON.parse(calls[0][1].body), HOME.proactive.gaps[0].follow_up);
});

test("a scan cut by its bound or by failed reads says so, never a clean 'No gaps found'", () => {
  const {context} = load();
  const shown = proactive => walk(context.homeFoundList(proactive)).map(node => node.textContent).join(" ");
  const cut = shown({status: "read", scanned: 20, gaps: [], truncated: true, unscanned: 160, max_objects: 20, unread: {store_unavailable: 3}});
  assert.ok(!cut.includes("No gaps found"), cut);
  for (const word of ["Scan incomplete", "160 found records unread", "3 reads failed", "store_unavailable: 3"]) assert.ok(cut.includes(word), word);
  const withGaps = shown({...HOME.proactive, truncated: true, unscanned: 5, max_objects: 20, unread: {}});
  assert.ok(withGaps.includes("Scan incomplete") && withGaps.includes("PO number is held empty."), withGaps);
  assert.ok(shown({status: "unreadable", error: "OperationalError", gaps: []}).includes("OperationalError"));
  assert.ok(shown({status: "read", scanned: 4, gaps: [], truncated: false, unread: {}}).includes("No gaps found"));
});

test("scheduled rules that did not run are named and the inbox is never read as clean", () => {
  const {context} = load();
  const shown = proactive => walk(context.homeFoundList(proactive)).map(node => node.textContent).join(" ");
  const base = {status: "read", scanned: 13, gaps: [], truncated: false, unread: {}, max_objects: null, unscanned: null};
  const unrun = shown({...base, complete: false, not_run: {card_rules: "not_configured", order_without_customer_po: "not_configured", receipt_without_qc: "blocked"},
    detectors: {card_rules: "not_configured", order_without_customer_po: "not_configured", receipt_without_qc: "blocked", expired_quote: "complete"}});
  assert.ok(!unrun.includes("No gaps found"), unrun);
  for (const word of ["Scan incomplete", "3 rules did not run", "orders with no customer PO (not run on this schedule)",
    "values missing on documents", "receipts with no quality check (a source it needs is not served)"]) assert.ok(unrun.includes(word), word);
  const cut = shown({...base, truncated: true, complete: false, not_run: {}, detectors: {order_without_customer_po: "truncated", expired_quote: "complete"}});
  assert.ok(!cut.includes("No gaps found") && cut.includes("1 rule stopped short of every record (orders with no customer PO)"), cut);
  assert.ok(!cut.includes("—"), cut);
  assert.ok(!shown({...base, complete: false, not_run: {}}).includes("No gaps found"));
  assert.ok(shown({...base, complete: true, not_run: {}}).includes("No gaps found"));
  const labelled = shown({...base, complete: true, not_run: {}, gaps: [{id: "pe-1", kind: "order_without_customer_po", summary: "S", citations: ["h"], follow_up: null}]});
  assert.ok(labelled.includes("Order with no customer PO"), labelled);
});

test("a scan that read 0 records is not a clean result; the objective box, then the scan, come first; a gap reads in plain words", () => {
  const {context} = load();
  const state = vm.runInContext("state", context);
  state.home = {cases: {status: "read", open_cases: 1, open_gaps: 1, cases: [{case_id: "case-1", question: "Q: PurchaseOrder SYNTH-1", gaps: [
    {id: "G1", state: "unknown", missing: "po_twin_of", why: "tier_floor", who_can_answer: ["buyer"], citations: [],
      plain: {title: "Copies of this purchase order", text: "Found only through weaker links.", who: ["Procurement"]}}]}]},
    proactive: {status: "read", scanned: 0, gaps: [], withheld_sources: [{source: "bc", label: "Business Central"}]}};
  const view = new Element("section");
  context.homeView(view);
  assert.deepEqual(view.children.map(node => node.dataset.home), ["objective", "proactive", "cases"]);
  const nodes = walk(view), shown = nodes.map(node => node.textContent).join(" ");
  assert.ok(!shown.includes("No gaps found"), shown);
  for (const word of ["Scanned 0 records you may read.", "not a clean result", "Business Central", "Purchase order SYNTH-1",
    "Copies of this purchase order", "Who can answer: Procurement"]) assert.ok(shown.includes(word), word);
  // The recorded codes stay, for an engineer, inside the engineer's detail only.
  const engineer = nodes.filter(node => node.className === "engineer").flatMap(walk);
  const outside = nodes.filter(node => !engineer.includes(node)).map(node => node.textContent).join(" ");
  assert.ok(!/po_twin_of|tier_floor/.test(outside), outside);
  assert.ok(engineer.some(node => node.textContent.includes("po_twin_of")));
});

test("the bearer survives a reload in this tab's storage and a refused one is forgotten", async () => {
  const stored = {};
  const sessionStorage = {getItem: key => stored[key] || null, setItem: (key, text) => { stored[key] = text; }, removeItem: key => { delete stored[key]; }};
  const sent = [];
  const fetch = async (url, options) => { sent.push(options.headers.Authorization); return {ok: false, status: 401, json: async () => ({error: "access_token_required"})}; };
  const document = {createElement: tag => new Element(tag), getElementById: () => new Element("div"), querySelectorAll: () => [], addEventListener: () => {}};
  const context = vm.createContext({document, URL, URLSearchParams, fetch, setTimeout, clearTimeout, sessionStorage});
  vm.runInContext(fs.readFileSync(path.join(__dirname, "app.js"), "utf8"), context);
  context.keepAccess("secret-1");
  assert.equal(context.storedAccess(), "secret-1");
  await assert.rejects(context.api("/api/session"), /Access token missing or invalid/);
  assert.deepEqual(sent, ["Bearer secret-1"]);
  assert.equal(context.storedAccess(), "");
  assert.equal(vm.runInContext("state.accessToken", context), "");
});

test("the objective box comes first, posts the objective and shows its cited answer, inputs, gaps and proposals", async () => {
  const proposal = {object_id: "Order:1", missing: "Confirm row.qty = 5 is still current", why: "no date", who_can_answer: ["PROC"], state: "unknown", priority: "high"};
  const result = {as_of: "2026-10-19", target: {date: "2026-11-15", source: "objective_text"},
    roots: [{object_id: "Order:1", object_type: "PurchaseOrder", found_by: "identifier"}],
    answer: {outcome: "answered", reason: "planner_answer", claims: [
      {text: "The order is open.", citations: [{l1_handle: H, openable: true}, {l1_handle: "withheld-h", openable: false}]}]},
    scenario: {inputs: [{field: "row.qty", value: "5", citation: H, openable: true, state: "unknown", source_date: null}],
      must_confirm: [{what: "row.qty = 5", why: "no date on its record", state: "unknown"}]},
    gaps: [{kind: "unknown", subject: "not in the record: freight quote", reason: "no record read answers it"}],
    follow_ups: [{proposal, status: "proposed_not_recorded"}]};
  const calls = [];
  const {context} = load(async (url, options) => { calls.push([url, options]); return {ok: true, json: async () => (url === "/api/objective" ? result : {})}; });
  const state = vm.runInContext("state", context);
  state.home = {...HOME, objective: {served: true, modes: ["investigate", "scenario"], max_chars: 1000}};
  state.token = "t";
  const view = new Element("section");
  context.homeView(view);
  assert.equal(view.children[0].dataset.home, "objective");
  const form = walk(view).find(node => node.tagName === "form");
  walk(form).find(node => node.tagName === "textarea").value = "  Air-ship PO 7 by Nov 15 ";
  walk(form).find(node => node.tagName === "select").value = "scenario";
  await form.listeners.submit({preventDefault() {}});
  assert.deepEqual([calls[0][0], JSON.parse(calls[0][1].body)], ["/api/objective", {objective: "Air-ship PO 7 by Nov 15", mode: "scenario"}]);
  const shown = walk(form).map(node => node.textContent).join(" ");
  for (const word of ["The order is open.", "PurchaseOrder · Order:1", "row.qty = 5", "date unknown", "A person must confirm", "freight quote", "Confirm row.qty"])
    assert.ok(shown.includes(word), word);
  // The citation the reader may not open renders as unavailable, never as an openable chip.
  assert.ok(walk(form).some(node => node.className === "case-chip withheld"));
  assert.ok(!walk(form).some(node => node.title === "withheld-h"));
  const propose = walk(form).find(node => node.textContent === "Propose follow-up");
  await propose.listeners.click();
  assert.deepEqual([calls[1][0], JSON.parse(calls[1][1].body)], ["/api/object-follow-up", proposal]);
  assert.equal(calls.length, 2);
});

test("a question has one path: it posts only /api/objective and shows each server verdict first, then the agent's answer", async () => {
  const late = {state: "late", text: "Late by 9 days: planned arrival 2025-10-04", subject: {object_id: "PurchaseOrder:1", label: "Purchase order PO-1"}, facts: [], follow_ups: []};
  const missing = {state: "missing", text: "Missing: receipt", subject: {object_id: "PurchaseOrder:1", label: "Purchase order PO-1"}, facts: [], follow_ups: []};
  const result = {as_of: "2026-10-19", target: {date: null}, roots: [{object_id: "PurchaseOrder:1", object_type: "PurchaseOrder", found_by: "identifier"}],
    answer: {outcome: "answered", reason: "planner_answer", claims: [{text: "The agent's own claim.", citations: [{l1_handle: H, openable: true}]}]},
    answers: [{object_id: "PurchaseOrder:1", object_type: "PurchaseOrder", parts: [
      {part: "late", family: "late_po", answer: late, why: [{text: "The plan read the order."}]},
      {part: "missing", answer: missing, why: []}, {part: "supplier", answer: null, why: []}]}],
    parts: {asked: ["late", "missing", "supplier", "margin"], left_to_agent: ["margin"], agent_called: true}, gaps: [], follow_ups: []};
  for (const [question, shape] of [["Why is PO-1 late, and what is missing?", result],
    ["Which purchase orders are late?", {...result, answers: [], parts: {asked: ["late"], left_to_agent: ["late"], agent_called: true}}]]) {
    const calls = [];
    const {context} = load(async (url, options) => { calls.push(url); return {ok: true, json: async () => (url === "/api/objective" ? shape : {})}; });
    const state = vm.runInContext("state", context);
    state.home = {...HOME, objective: {served: true, modes: ["investigate", "scenario"], max_chars: 1000}};
    state.token = "t";
    const view = new Element("section");
    context.homeView(view);
    const form = walk(view).find(node => node.tagName === "form");
    walk(form).find(node => node.tagName === "textarea").value = question;
    await form.listeners.submit({preventDefault() {}});
    // No router: never /api/objects, /api/investigate or /api/object from the browser.
    assert.deepEqual(calls, ["/api/objective"], question);
    const shown = walk(form).find(node => node.dataset.objective === "result");
    const cards = shown.children.filter(node => node.dataset.objective === "answer");
    const verdicts = cards.map(card => [card.children[0].dataset.verdict, card.children[0].textContent]);
    const left = shown.children.find(node => node.dataset.objective === "left");
    if (shape.answers.length) {
      assert.deepEqual(verdicts, [["late", late.text], ["missing", missing.text],
        ["not_established", "Not established: the server returned no answer for this part"]]);
      // The verdicts come before the agent's own answer, and the part left to it is named.
      const first = shown.children.indexOf(cards[0]);
      const claim = shown.children.findIndex(node => walk(node).some(n => n.textContent === "The agent's own claim."));
      assert.ok(first === 0 && claim > shown.children.indexOf(cards[2]), `${first} ${claim}`);
      assert.equal(left.textContent, "Left to the agent: margin (no server answer covers it for the records named).");
    } else {
      assert.deepEqual(verdicts, []);
      assert.equal(left.textContent, "Left to the agent: lateness (the question names no single record by its number).");
    }
    assert.ok(walk(form).some(node => node.textContent === "The agent's own claim."));
  }
});

test("Propose says when the follow-up cannot request approval and why; a disabled Propose says why in text", async () => {
  const {context} = load(async () => ({ok: true, json: async () => ({proposal: {case_id: "case-9", proposal_id: "P3",
    not_prepared: "mc_operator_not_configured"}})}));
  const W = require("./workspace.js");
  context.PLXWorkspace = W;
  const state = vm.runInContext("state", context);
  state.home = {cases: {status: "read", cases: []}, proactive: {...HOME.proactive, as_of: "2099-01-05T00:00:00.123456Z",
    gaps: [HOME.proactive.gaps[0], {...HOME.proactive.gaps[0], id: "pg-2", follow_up: null}]}};
  state.token = "t";
  const view = new Element("section");
  context.homeView(view);
  const shown = () => walk(view).map(node => node.textContent).join(" ");
  assert.ok(shown().includes("as of Jan 4, 2099, 7:00 PM ET."), shown());
  assert.ok(shown().includes("Nothing to propose: this gap carries no follow-up."));
  await walk(view).find(node => node.textContent === "Propose follow-up").listeners.click();
  assert.ok(shown().includes("Recorded on case case-9, but it cannot request approval yet: this console has no Mission Control operator set up. Nothing was executed."), shown());
  assert.ok(!shown().includes("waits for a person's approval"));
});

test("a route in the address: a case link opens the case, Find & open keeps its record, unknown keys are dropped", () => {
  const {context} = load();
  const plain = route => JSON.parse(JSON.stringify(context.parseRoute(route)));
  assert.deepEqual(plain("#case=case-1"), {view: "cases", caseId: "case-1", objectRoute: {}, access: null});
  assert.deepEqual(plain("#view=cases&case=case-1"), {view: "cases", caseId: "case-1", objectRoute: {}, access: null});
  assert.deepEqual(plain("#view=objects&q=PO5447&object=X%3A1&evil=1&case=c"),
    {view: "objects", caseId: null, objectRoute: {q: "PO5447", object: "X:1"}, access: null});
  assert.deepEqual(plain("#view=nowhere"), {view: "home", caseId: null, objectRoute: {}, access: null});
  assert.deepEqual(plain("#view=home&case=c"), {view: "home", caseId: null, objectRoute: {}, access: null});
  assert.equal(plain("#access=abc&view=home").access, "abc");
});
