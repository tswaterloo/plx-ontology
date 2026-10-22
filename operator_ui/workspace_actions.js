"use strict";
// Act on a registered action from the object workspace (WS-11). Under each opened object's
// "Registered actions" section (rendered by workspace.js) this lists the intents that cite
// the object's records with the reader's next legal step (/api/object-actions) and takes
// one step per click (POST /api/object-action with the session's CSRF token): request
// approval, check the decision, dispatch (once; a repeat replays and never re-sends) or
// verify by read-back. Nothing here approves: a person other than whoever the action is
// for approves the exact call, in Mission Control or on the call itself. An uncertain
// outcome is shown as uncertain, never as done. A reader without the action role, or who
// cannot see an intent's evidence, gets no control. A console that takes no steps answers
// served:false and the panel is not shown; a failed load shows nothing either, never a
// raw error code on the page. Server text is set as textContent.
// The console's own api/post (operator_ui/app.js, page globals) carry the session; attach
// takes them explicitly.
(function (root) {
  const LIST_ROUTE = "/api/object-actions?id=";
  const ACT_ROUTE = "/api/object-action";
  const OUTCOME_TEXT = {
    verified: "Done: the effect was verified by read-back.",
    unknown: "Uncertain: sent, but the effect is not verified. Not done; verify it, never resend.",
    failed: "Not applied: the owning system did not apply it.",
    not_dispatched: "Nothing has been sent."};

  function words(text) { return String(text || "").replace(/_/g, " "); }
  function list(v) { return Array.isArray(v) ? v : []; }

  function dom(doc) {
    function el(tag, cls, text) {
      const n = doc.createElement(tag);
      if (cls) n.className = cls;
      if (text !== undefined && text !== null) n.textContent = String(text);
      return n;
    }
    function add(parent, ...children) { children.filter(Boolean).forEach(c => parent.appendChild(c)); return parent; }
    return {el, add};
  }

  // One intent: what it is, its state and outcome, and the reader's next step (if any).
  // take(body) posts one step and resolves to the server's answer.
  function intentRow(d, intent, take) {
    const row = d.el("div", "ow-action");
    row.setAttribute("data-intent", intent.intent_id || "");
    d.add(row, d.el("strong", "", words(intent.capability || "action")),
      d.el("span", "ow-reason", ` · case ${intent.case_id || "?"} ${intent.proposal_id || ""} · ${words(intent.state)}`),
      d.el("p", intent.outcome === "verified" ? "ow-ok" : "ow-muted", OUTCOME_TEXT[intent.outcome] || `Outcome: ${words(intent.outcome || "unknown")}`));
    const control = intent.control;
    if (!control) return row;   // no action role, or not the reader's to act on
    if (!control.step || !control.available) {
      row.appendChild(d.el("p", "ow-muted", `No step for you now: ${words(control.why || "none")}.`));
      return row;
    }
    let task = null;
    if (control.needs_task_id) {
      task = d.el("input");
      task.setAttribute("placeholder", "Mission Control task, e.g. TASK-123");
      task.setAttribute("aria-label", "Mission Control task for the approval request");
      row.appendChild(task);
    }
    const button = d.el("button", "button subtle", control.label || words(control.step));
    button.type = "button";
    button.setAttribute("data-step", control.step);
    const said = d.el("small", "ow-review-status");
    button.addEventListener("click", async () => {
      button.disabled = true;
      said.textContent = "Working…";
      const body = {intent_id: intent.intent_id, step: control.step};
      if (task) body.task_id = String(task.value || "").trim();
      try {
        const data = await take(body);
        said.textContent = `${words(data.state)}: ${OUTCOME_TEXT[data.outcome] || words(data.outcome)}`;
      } catch (error) {
        said.textContent = "Not done: " + (error && error.message ? error.message : String(error));
      } finally { button.disabled = false; }
    });
    d.add(row, button, said);
    return row;
  }

  // The /api/object-actions answer as one panel, or null when this console takes no steps
  // (served:false): the read-only intents already show in "Registered actions".
  function renderActions(doc, data, take) {
    if (!data || data.served === false) return null;
    const d = dom(doc);
    const box = d.el("div", "ow-actions");
    box.setAttribute("data-extension", "actions");
    d.add(box, d.el("h3", "", "Act on these actions"));
    const intents = (data && data.intents) || {};
    if (intents.status !== "read") {
      box.appendChild(d.el("p", "ow-muted", "No action store is configured for this console."));
      return box;
    }
    list(intents.intents).forEach(intent => box.appendChild(intentRow(d, intent, take)));
    if (!list(intents.intents).length) box.appendChild(d.el("p", "ow-muted", "No registered action you can see cites this object's records."));
    box.appendChild(d.el("p", "ow-note", intents.note || data.note || "This console never approves."));
    return box;
  }

  // Load and show the panel for one opened object; reload it after every step.
  // A panel that is not served, or that fails to load, leaves the slot empty: the section
  // above it already lists the intents read-only.
  async function show(doc, slot, objectId, io) {
    const clear = () => { while (slot.firstChild) slot.removeChild(slot.firstChild); slot.textContent = ""; };
    const load = async () => {
      const data = await io.api(LIST_ROUTE + encodeURIComponent(objectId));
      clear();
      const panel = renderActions(doc, data, async body => {
        const answer = await io.post(ACT_ROUTE, body);
        load().catch(clear);
        return answer;
      });
      if (panel) slot.appendChild(panel);
    };
    try { await load(); } catch (error) { clear(); }
  }

  // Watch target (the console's view) for opened objects; add the panel to each one's
  // "Registered actions" section.
  function attach(target, io) {
    if (!target || !io || typeof io.api !== "function" || typeof io.post !== "function") return null;
    const doc = target.ownerDocument || root.document;
    const seen = new WeakSet();
    const scan = () => target.querySelectorAll(".ow-workspace[data-object-id]").forEach(box => {
      if (seen.has(box)) return;
      seen.add(box);
      const objectId = box.getAttribute("data-object-id");
      const section = box.querySelector('[data-section="intents"]');
      if (!objectId || !section) return;
      const slot = doc.createElement("div");
      section.appendChild(slot);
      show(doc, slot, objectId, io);
    });
    const observer = new root.MutationObserver(scan);
    observer.observe(target, {childList: true, subtree: true});
    scan();
    return observer;
  }

  const api = {renderActions, show, attach, OUTCOME_TEXT};
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  root.PLXWorkspaceActions = api;
  if (root.document && root.MutationObserver) {
    const boot = () => attach(root.document.getElementById("view"), {api: root.api, post: root.post});
    if (root.document.readyState === "loading") root.document.addEventListener("DOMContentLoaded", boot);
    else boot();
  }
})(typeof globalThis !== "undefined" ? globalThis : this);
