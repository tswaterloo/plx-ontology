"use strict";
// The confirm-a-missing-link panel (operator_ui/link_confirm.js) over contract-shaped
// /api/link-confirm answers: no network (fetch throws), a minimal DOM stand-in.

const assert = require("node:assert/strict");
const test = require("node:test");

globalThis.fetch = () => { throw new Error("no network in UI unit tests"); };
const L = require("./link_confirm.js");

class Element {
  constructor(tagName) {
    this.tagName = tagName; this.children = []; this.textContent = ""; this.className = "";
    this.listeners = {}; this.attributes = {}; this.value = "";
  }
  appendChild(child) { this.children.push(child); return child; }
  removeChild(child) { this.children.splice(this.children.indexOf(child), 1); return child; }
  get firstChild() { return this.children[0] || null; }
  addEventListener(event, handler) { this.listeners[event] = handler; }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return this.attributes[name] === undefined ? null : this.attributes[name]; }
}
const doc = {createElement: tag => new Element(tag)};
function walk(node) { return [node, ...node.children.flatMap(walk)]; }
function text(node) { return walk(node).map(n => n.textContent).join(" "); }
function button(node, label) { return walk(node).find(n => n.tagName === "button" && n.textContent === label); }

const PO = "PurchaseOrder:portal.app_purchase_order:{\"id\":\"1\"}";
const REVIEW = {decision: "accept_link", subject: "SupplierConfirmation:x", target: PO,
  link_type: "SupplierConfirmation.confirms", evidence: [{l1_handle: "h", digest: "d", field_path: "f"}]};
const LINK = {review_id: "rev-1", decision: "accept_link", link_type: "SupplierConfirmation.confirms",
  author: {principal: "fixture:review:identity"}, recorded_at: "2026-10-19T12:00:00Z"};

test("a person confirms exactly the proposed link, sees who and when, and can undo it", async () => {
  const posted = [];
  let links = [];
  const send = async body => {
    posted.push(body);
    if (body.mode === "prepare_link") return {status: "ready", review: REVIEW, says: "Confirm that the PI belongs to the PO."};
    if (body.mode === "confirm") { links = [LINK]; return {status: "confirmed", review: LINK, says: "Confirmed by fixture at 12:00."}; }
    if (body.mode === "undo") { links = []; return {status: "undone", says: "Undone."}; }
    return {status: "links", links};
  };
  const panel = L.renderPanel(doc, PO, send, []);
  walk(panel).find(n => n.tagName === "input").value = " PI-1 ";
  await button(panel, "Link this document").listeners.click();
  assert.deepEqual(posted[0], {mode: "prepare_link", record: PO, document: "PI-1"});
  assert.match(text(panel), /Confirm that the PI belongs/);
  assert.equal(posted.length, 1, "preparing records nothing");
  await button(panel, "Confirm").listeners.click();
  assert.deepEqual(posted[1], {mode: "confirm", review: REVIEW});
  assert.match(text(panel), /Link confirmed \(SupplierConfirmation.confirms\) by fixture:review:identity at 2026-10-19/);
  await button(panel, "Undo").listeners.click();
  assert.deepEqual(posted[3], {mode: "undo", record: PO, review_id: "rev-1"});
  assert.match(text(panel), /No confirmed link/);
});

test("a refusal reads in plain words, never as a code, and a failed load shows no panel", async () => {
  const plain = "You may not confirm links between records: that needs the identity review grant (review:identity).";
  const send = async body => {
    if (body.mode === "prepare_link") return {status: "ready", review: REVIEW, says: "Confirm?"};
    throw new Error(plain);
  };
  const panel = L.renderPanel(doc, PO, send, []);
  walk(panel).find(n => n.tagName === "input").value = "PI-1";
  await button(panel, "Link this document").listeners.click();
  await button(panel, "Confirm").listeners.click();
  assert.match(text(panel), /needs the identity review grant/);
  const coded = L.renderPanel(doc, PO, async () => { throw new Error("review_not_served"); }, []);
  walk(coded).find(n => n.tagName === "input").value = "PI-1";
  await button(coded, "Link this document").listeners.click();
  assert.doesNotMatch(text(coded), /review_not_served/);
  const slot = new Element("div");
  await L.show(doc, slot, PO, {post: async () => { throw new Error("review_not_served"); }});
  assert.equal(slot.children.length, 0);
});
