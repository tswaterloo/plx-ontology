"use strict";
// Confirm a missing link from the object workspace (operating/human_links.py). Under each
// opened object's "Gaps" section (rendered by workspace.js) a person names the document a
// blocked case is missing ("this PI belongs to this PO") or the supplier number a document
// states ("this supplier number is this PO"); the console proposes the one plx.review
// decision (prepare_link / prepare_alias: nothing is written), the person confirms it
// (POST /api/link-confirm with the session's CSRF token), and the links in force are listed
// with who and when, each with Undo. Nothing here links on its own: every confirmation is the
// person's click, recorded by plx.review as that session (review:identity required; a refusal
// is shown in plain words). A console that records no reviews, or a failed load, shows no
// panel. Server text is set as textContent.
(function (root) {
  const ROUTE = "/api/link-confirm";
  const CODE = /^[a-z0-9_:.-]+$/;

  function list(v) { return Array.isArray(v) ? v : []; }

  function dom(doc) {
    function el(tag, cls, text) {
      const n = doc.createElement(tag);
      if (cls) n.className = cls;
      if (text !== undefined && text !== null) n.textContent = String(text);
      return n;
    }
    function add(parent, ...children) { children.filter(Boolean).forEach(c => parent.appendChild(c)); return parent; }
    function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); return node; }
    return {el, add, clear};
  }

  // A refusal's plain sentence (the server's detail), never a bare code.
  function said(error) {
    const message = error && error.message ? String(error.message) : "";
    return message && !CODE.test(message) ? message : "The link was not recorded.";
  }

  function linkLine(link) {
    const what = link.decision === "alias" ? "Supplier number confirmed" : "Link confirmed";
    const who = (link.author && link.author.principal) || "someone";
    return `${what}${link.link_type ? " (" + link.link_type + ")" : ""} by ${who} at ${link.recorded_at || "an unknown time"}`;
  }

  // The panel over one record. send(body) posts one request and resolves to its link_confirm
  // answer (rejecting with the server's plain sentence on a refusal).
  function renderPanel(doc, objectId, send, links) {
    const d = dom(doc);
    const box = d.el("div", "ow-link-confirm");
    d.add(box, d.el("h4", null, "Confirm a missing link"),
      d.el("p", "ow-muted", "Name the document this record is missing, or the supplier number it states. " +
        "Nothing is linked until you confirm; your name and the time are recorded and you can undo it. " +
        "Confirming needs the identity review grant (review:identity)."));
    const documentInput = d.el("input");
    documentInput.setAttribute("placeholder", "document number or id");
    const numberInput = d.el("input");
    numberInput.setAttribute("placeholder", "supplier number (optional)");
    const proposal = d.el("div", "ow-link-proposal");
    const status = d.el("p", "ow-link-status");
    const held = d.el("ul", "ow-link-list");

    function showLinks(items) {
      d.clear(held);
      if (!list(items).length) held.appendChild(d.el("li", "ow-muted", "No confirmed link on this record."));
      list(items).forEach(link => {
        const row = d.el("li", "ow-link", linkLine(link));
        const undo = d.el("button", null, "Undo");
        undo.setAttribute("data-review-id", link.review_id);
        undo.addEventListener("click", () => send({mode: "undo", record: objectId, review_id: link.review_id})
          .then(answer => { status.textContent = answer.says; return refresh(); })
          .catch(error => { status.textContent = said(error); }));
        held.appendChild(d.add(row, undo));
      });
    }
    function refresh() {
      return send({mode: "links", record: objectId}).then(answer => showLinks(answer.links))
        .catch(error => { status.textContent = said(error); });
    }
    function prepare(mode) {
      const document = String(documentInput.value || "").trim();
      const number = String(numberInput.value || "").trim();
      d.clear(proposal);
      if (!document) { status.textContent = "Enter the document's number or id first."; return Promise.resolve(); }
      const body = {mode, record: objectId, document};
      if (mode === "prepare_alias" && number) body.number = number;
      return send(body).then(answer => {
        status.textContent = "";
        if (answer.status !== "ready") { status.textContent = answer.says || "The link was not recorded."; return; }
        const confirm = d.el("button", null, "Confirm");
        confirm.setAttribute("data-decision", answer.review.decision);
        confirm.addEventListener("click", () => send({mode: "confirm", review: answer.review})
          .then(done => { d.clear(proposal); status.textContent = done.says; return refresh(); })
          .catch(error => { status.textContent = said(error); }));
        d.add(proposal, d.el("p", null, answer.says), confirm);
      }).catch(error => { status.textContent = said(error); });
    }
    const linkButton = d.el("button", null, "Link this document");
    linkButton.setAttribute("data-mode", "prepare_link");
    linkButton.addEventListener("click", () => prepare("prepare_link"));
    const aliasButton = d.el("button", null, "Its supplier number is this record");
    aliasButton.setAttribute("data-mode", "prepare_alias");
    aliasButton.addEventListener("click", () => prepare("prepare_alias"));
    d.add(box, documentInput, numberInput, linkButton, aliasButton, proposal, status, held);
    showLinks(links);
    return box;
  }

  // The panel once the console answers the record's links; nothing when it records no
  // reviews or the load fails.
  function show(doc, slot, objectId, io) {
    const send = body => io.post(ROUTE, body).then(answer => answer.link_confirm);
    return send({mode: "links", record: objectId})
      .then(answer => { slot.appendChild(renderPanel(doc, objectId, send, answer.links)); })
      .catch(() => {});
  }

  function attach(target, io) {
    if (!target || !io || typeof io.post !== "function") return null;
    const doc = target.ownerDocument || root.document;
    const seen = new WeakSet();
    const scan = () => target.querySelectorAll(".ow-workspace[data-object-id]").forEach(box => {
      if (seen.has(box)) return;
      seen.add(box);
      const objectId = box.getAttribute("data-object-id");
      const section = box.querySelector('[data-section="gaps"]');
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

  const api = {renderPanel, show, attach};
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  root.PLXLinkConfirm = api;
  if (root.document && root.MutationObserver) {
    const boot = () => attach(root.document.getElementById("view"), {post: root.post});
    if (root.document.readyState === "loading") root.document.addEventListener("DOMContentLoaded", boot);
    else boot();
  }
})(typeof globalThis !== "undefined" ? globalThis : this);
