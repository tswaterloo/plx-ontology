"use strict";
// The act control (operator_ui/workspace_actions.js) over contract-shaped
// /api/object-actions answers: no network (fetch throws), a minimal DOM stand-in.

const assert = require("node:assert/strict");
const test = require("node:test");

globalThis.fetch = () => { throw new Error("no network in UI unit tests"); };
const A = require("./workspace_actions.js");

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
function rows(node) { return walk(node).filter(n => n.className === "ow-action"); }
function buttons(node) { return walk(node).filter(n => n.tagName === "button"); }

const IID = "act_" + "0".repeat(24);
const intent = (over) => ({intent_id: IID, case_id: "case_1", proposal_id: "P1",
  capability: "mc.task.create_follow_up", state: "call_approved", outcome: "not_dispatched",
  control: {step: "dispatch", label: "Dispatch (once)", available: true, why: null,
    needs_task_id: false, outcome: "not_dispatched", done: false}, ...over});
const answer = (...intents) => ({intents: {status: "read", intents, note: "this console never approves"}});

test("a step the reader may take posts exactly that step for that intent", async () => {
  const posted = [];
  const panel = A.renderActions(doc, answer(intent()), async body => {
    posted.push(body);
    return {state: "dispatched_unknown", outcome: "unknown"};
  });
  const [button] = buttons(panel);
  assert.equal(button.getAttribute("data-step"), "dispatch");
  await button.listeners.click();
  assert.deepEqual(posted, [{intent_id: IID, step: "dispatch"}]);
  // An uncertain dispatch reads as uncertain, never as done.
  const said = text(panel);
  assert.match(said, /Uncertain/);
  assert.doesNotMatch(said, /Done/);
});

test("an approval request on an owner-system action names its Mission Control task", async () => {
  const posted = [];
  const panel = A.renderActions(doc, answer(intent({capability: "fixture.record.set_field",
    state: "proposed", control: {step: "request_approval", label: "Request approval",
      available: true, why: null, needs_task_id: true}})), async body => { posted.push(body); return {}; });
  const input = walk(panel).find(n => n.tagName === "input");
  input.value = " TASK-9 ";
  await buttons(panel)[0].listeners.click();
  assert.deepEqual(posted, [{intent_id: IID, step: "request_approval", task_id: "TASK-9"}]);
});

test("no control, or no step now, renders no button; a withheld reader sees nothing to press", () => {
  const panel = A.renderActions(doc, answer(
    intent({control: null}),
    intent({state: "call_approval_required", control: {step: null, available: false,
      why: "awaiting another person's approval of the exact call shown"}}),
    intent({state: "verified", outcome: "verified", control: null})), () => { throw new Error("never"); });
  assert.equal(rows(panel).length, 3);
  assert.equal(buttons(panel).length, 0);
  assert.match(text(panel), /No step for you now: awaiting another person's approval/);
  assert.match(text(panel), /Done: the effect was verified/);
  const none = A.renderActions(doc, {intents: {status: "not_configured"}}, () => {});
  assert.equal(buttons(none).length, 0);
});

test("a console that takes no steps shows no actions panel and never a raw error code", async () => {
  // /api/object-actions answers served:false: nothing to render, no error on the page.
  assert.equal(A.renderActions(doc, {served: false, read_only: true, object_id: "PurchaseOrder:x"}, () => {}), null);
  const unserved = new Element("div");
  await A.show(doc, unserved, "PurchaseOrder:x", {api: async () => ({served: false, read_only: true}), post: async () => ({})});
  assert.equal(unserved.children.length, 0);
  assert.equal(text(unserved).trim(), "");
  // An older server's 501 (app.js api() throws the code): the slot stays empty.
  const refused = new Element("div");
  await A.show(doc, refused, "PurchaseOrder:x", {api: async () => { throw new Error("actions_not_served"); }, post: async () => ({})});
  assert.equal(refused.children.length, 0);
  assert.doesNotMatch(text(refused), /actions_not_served|unavailable/i);
  // A served console still shows its panel.
  const served = new Element("div");
  await A.show(doc, served, "PurchaseOrder:x", {api: async () => ({served: true, ...answer(intent())}), post: async () => ({})});
  assert.equal(buttons(served).length, 1);
});

test("an approval shows exactly what is approved before the click and names its digest", async () => {
  const posted = [];
  const approves = {intent_id: IID, action_type: "portal.sales_order.set_customer_po",
    owner_system: "portal", target: {relation: "public.app_sales_order", key: {id: "so-1"}},
    expected_effect: {fields: ["po_reference"], values: {po_reference: "PO7001"}},
    parameters: [{name: "proposed_customer_po", source: {source: "citation",
      citation: "fixture://doc/101", field: "customer_po"}}],
    evidence: [{handle: "fixture://doc/101", digest: "ab".repeat(32)}],
    human_preconditions: ["a person confirms the PO belongs to this order"],
    call: {tool: "mc_request_approval", arguments: {taskId: "TASK-9", reason: "why"}}};
  const sha = "c".repeat(64);
  const panel = A.renderActions(doc, answer(intent({state: "gate_prepared", control: {
    step: "approve_call", label: "Approve exactly this call", available: true, why: null,
    approves, approves_sha256: sha}})), async body => { posted.push(body); return {state: "call_approved", outcome: "not_dispatched"}; });
  const shown = text(panel);
  for (const part of [/portal\.sales_order\.set_customer_po in portal/, /public\.app_sales_order \{"id":"so-1"\}/,
    /proposed_customer_po: from fixture:\/\/doc\/101 field customer_po/, /PO7001/,
    /mc_request_approval \{"taskId":"TASK-9","reason":"why"\}/, /nothing is sent by approving/,
    /Check first: a person confirms/]) assert.match(shown, part);
  const [button] = buttons(panel);
  assert.equal(button.getAttribute("data-step"), "approve_call");
  await button.listeners.click();
  assert.deepEqual(posted, [{intent_id: IID, step: "approve_call", approves_sha256: sha}]);
  assert.match(text(panel), /call approved: Nothing has been sent/);
});

test("an approval whose call is not shown offers no button", () => {
  const panel = A.renderActions(doc, answer(intent({state: "gate_prepared", control: {
    step: "approve_call", label: "Approve", available: true, why: null, approves: null,
    approves_sha256: null}})), () => { throw new Error("never"); });
  assert.equal(buttons(panel).length, 0);
  assert.match(text(panel), /the call to approve is not shown/);
});
