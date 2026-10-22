"use strict";
// Console label bugs: list rows keep title and sub-label apart, the relationship-map
// caption is computed from the links shown, object types keep word breaks, and Back works.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

class Element {
  constructor(tagName) {
    this.tagName = tagName; this.children = []; this.textContent = ""; this.className = ""; this.dataset = {};
    this.listeners = {}; this.hidden = false; this.attributes = {}; this.parentNode = null;
    const set = new Set();
    this.classList = {contains: c => set.has(c), add: c => set.add(c), remove: c => set.delete(c), toggle: () => {}};
  }
  appendChild(c) { c.parentNode = this; this.children.push(c); return c; }
  removeChild(c) { this.children.splice(this.children.indexOf(c), 1); return c; }
  addEventListener(e, h) { this.listeners[e] = h; }
  setAttribute(n, v) { this.attributes[n] = String(v); }
  getAttribute(n) { return this.attributes[n] === undefined ? null : this.attributes[n]; }
  get firstChild() { return this.children[0] || null; }
  get firstElementChild() { return this.children[0] || null; }
}
function walk(n) { return [n, ...n.children.flatMap(walk)]; }
function load() {
  const ids = {};
  const document = {createElement: t => new Element(t), getElementById: id => (ids[id] = ids[id] || new Element("div")),
    querySelectorAll: () => [], addEventListener: () => {}};
  const context = vm.createContext({document, URL, URLSearchParams, fetch: async () => { throw new Error("no network"); }, setTimeout});
  vm.runInContext(fs.readFileSync(path.join(__dirname, "app.js"), "utf8"), context);
  return {context, ids};
}
const CASE = {case_id: "AO-26013C16", question: "Is it late?", status: "open", events: 2, updated_at: "2099-01-02T10:00:00Z", gaps: [{id: "G1"}]};

function assertSeparated(rowNode, caseId) {
  const title = walk(rowNode).find(n => n.className === "row-title");
  const sub = walk(rowNode).find(n => n.className === "row-sub");
  assert.ok(title && sub && title !== sub && title.parentNode === sub.parentNode);
  // Block elements: neither is an inline span running into the other.
  assert.notEqual(title.tagName, "span"); assert.notEqual(sub.tagName, "span");
  for (const n of [title, sub]) assert.ok(!new RegExp(caseId + "[A-Za-z0-9]").test(n.textContent), n.textContent);
  assert.ok(!title.textContent.includes("gaps") && !title.textContent.includes("question"));
}

test("home and case rows render title and sub-label as separate blocks", () => {
  const {context} = load();
  const state = vm.runInContext("state", context);
  const home = context.homeCaseList({status: "read", cases: [CASE]});
  assertSeparated(walk(home).find(n => n.className === "list-row"), "AO-26013C16");
  state.cases = {available: true, cases: [{...CASE, case_id: "PO54477"}]};
  const target = new Element("section");
  context.casesView(target);
  const r = walk(target).find(n => n.className === "list-row");
  assertSeparated(r, "PO54477");
  assert.ok(walk(r).some(n => /question\(s\)/.test(n.textContent)));
});

test("Back is visible on a first detail view and returns to the list", async () => {
  const {context, ids} = load();
  const a = new Element("div"), b = new Element("div");
  context.openDetail(a);
  assert.equal(ids["back-detail"].hidden, false);
  await context.backDetail();
  assert.ok(ids["detail"].classList.contains("hidden"));
  assert.equal(ids["detail-body"].children.length, 0);
  context.openDetail(a); context.openDetail(b);
  assert.equal(ids["back-detail"].hidden, false);
  await context.backDetail();
  assert.equal(ids["detail-body"].children[0], a);
});

const W = (() => { globalThis.fetch = () => { throw new Error("no network"); }; return require("./workspace.js"); })();
const doc = {createElement: t => new Element(t), createElementNS: (ns, t) => new Element(t)};
const ROOT = 'Header:portal.sample_header:{"id":7}', LINE = 'Line:portal.sample_line:{"id":71}';
function data(tiers, type) {
  return {read_only: true, status: "read", object_id: ROOT, evidence: {label: "isolated-fixture", fixture: true},
    card: {object: {object_id: ROOT, object_type: type || "Header", display: null}, properties: [], conflicts: [], identity: [], linked_counts: [], gaps: [], completeness: {complete: true}},
    around: {tier_floor: "contradicted", truncated: false, hops: tiers.map((tier, i) => ({link_type: "Line.line_of", direction: "reverse", result: {
      objects: [{object_id: LINE + i, object_type: "BottleVendor"}],
      edges: [{link_id: "L" + i, link_type: "Line.line_of", from: LINE + i, to: ROOT, tier, reason: [], why_not: []}], gaps: [], completeness: {complete: true}}}))}};
}
function caption(tiers) {
  const node = W.renderWorkspace(doc, data(tiers), {openable: false});
  const s = walk(node).find(n => n.attributes["data-section"] === "around");
  return walk(s).map(n => n.textContent).join(" ");
}
test("relationship map captions the weakest tier of the links shown", () => {
  assert.match(caption(["verified", "verified"]), /weakest shown: verified/i);
  assert.doesNotMatch(caption(["verified"]), /contradicted/i);
  assert.match(caption(["verified", "lead", "reviewed"]), /weakest shown: lead/i);
  assert.match(caption(["verified", "contradicted"]), /weakest shown: contradicted/i);
  assert.match(caption([]), /weakest shown: no links/i);
  assert.match(caption(["verified", "mystery"]), /weakest shown: mystery/i);
});
test("object types keep their word break", () => {
  const node = W.renderWorkspace(doc, data(["verified"], "BottleVendor"), {openable: false});
  const all = walk(node).map(n => n.textContent).join(" ");
  assert.ok(all.includes("Bottle Vendor") && !all.includes("BottleVendor"), all.slice(0, 300));
});
