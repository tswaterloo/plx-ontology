"use strict";
// Operator console shell: the landing "Find & open" view (operator_ui/workspace.js, over the
// object routes) and the read-only case workspace (/api/cases, /api/case/<id>). Every route
// called here is served by operator_ui/server.py (tests/test_operator_ui_routes.py checks it).

const state = {view: "home", home: null, token: "", accessToken: "", sessionVerified: false, cases: {available: false, cases: []},
  caseView: null, caseWorkspace: false, detailTrail: [], detailRestoring: false, objectRoute: {}, objects: null, proposed: {}};
const labels = {
  home: ["Home", "Your open gaps and cases.", "What is still unanswered on the cases you may read, and the gaps the workbench found by rule in the records you may read, each cited. Nothing is sent from here."],
  objects: ["Find & open", "Ask about any transaction.", "Start from a question or an identifier, open the record, and read its card, links, timeline, process, gaps, documents and registered actions, each value cited."],
  cases: ["Cases", "Work one case with its evidence.", "Timeline, originals, relationships, gaps and recorded proposals of a durable investigation, read for you with every citation re-checked."]};
const $ = (id) => document.getElementById(id);
function el(tag, cls, text) { const n = document.createElement(tag); if (cls) n.className = cls; if (text !== undefined && text !== null) n.textContent = String(text); return n; }
function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); return node; }
function append(parent, ...children) { children.filter(Boolean).forEach(n => parent.appendChild(n)); return parent; }
function value(v) { if (v === null || v === undefined || v === "") return "—"; if (typeof v === "object") return JSON.stringify(v); return String(v); }
function array(v) { return Array.isArray(v) ? v : []; }
function title(item) { return item.title || item.label || item.name || item.summary || item.id || item.entity_id || item.plan_id || "Untitled"; }
function tag(text) { const t = String(text || "unknown"); const c = /block|fail|critical|high|uncertain|error/i.test(t) ? "red" : /warn|pending|propos|review|medium|stale/i.test(t) ? "amber" : /info|low|read/i.test(t) ? "blue" : ""; return el("span", "tag " + c, t); }
function panel(name, hint, body) { const n = el("section", "panel"); const h = el("div", "panel-head"); append(h, el("h2", "", name), el("small", "", hint)); append(n, h, append(el("div", "panel-body"), body)); return n; }
function empty(message, detail) { const n = el("div", "empty-state"); append(n, el("strong", "", message), el("span", "", detail)); return n; }
function fields(data, exclude = []) { const box = el("div", "field-list"); if (!data || typeof data !== "object") return append(box, el("span", "text-muted", value(data))); Object.entries(data).filter(([key]) => !exclude.includes(key)).forEach(([key, val]) => { const row = el("div", "field"); append(row, el("div", "field-label", key.replace(/_/g, " ")), el("div", "field-value", value(val))); box.appendChild(row); }); return box; }
function row(item, mark, sub, right, action) { const b = el("button", "list-row"); b.type = "button"; append(b, el("span", "row-mark", mark), append(el("div", "row-main"), el("div", "row-title", title(item)), el("div", "row-sub", sub)), el("span", "row-right", right || "Open →")); b.addEventListener("click", action); return b; }
function list(items, build, noItems, detail) { if (!items.length) return empty(noItems, detail); const box = el("div", "stack"); items.forEach(item => box.appendChild(build(item))); return box; }
function notice(message) { $("notice").textContent = message || ""; }
async function api(path, options) { const headers = {...(options && options.headers || {})}; if (state.accessToken) headers.Authorization = `Bearer ${state.accessToken}`; const res = await fetch(path, {cache: "no-store", ...options, headers}); let data; try { data = await res.json(); } catch { throw new Error("Service returned an unreadable response"); } if (!res.ok) { if (res.status === 401) { keepAccess(""); state.sessionVerified = false; throw new Error("Access token missing or invalid. Reopen the console URL printed in the local terminal."); } throw new Error(data.detail || data.error || `HTTP ${res.status}`); } return data; }
// The session survives a reload and a bookmark. The bearer (the fixture access token or a
// live session ticket) is kept in this tab's sessionStorage: same origin, never in the URL,
// never a cookie (a cookie would be sent to every localhost port). A new tab of this console
// (a bookmark) asks the open tabs over a same-origin BroadcastChannel; only a tab whose
// bearer the server accepted answers. The server verifies every request exactly as before,
// and a 401 forgets the bearer.
const ACCESS_KEY = "plx.console.access", ACCESS_CHANNEL = "plx-console-access";
function accessStore() { try { return globalThis.sessionStorage || null; } catch { return null; } }
function keepAccess(token) {
  state.accessToken = token || "";
  const store = accessStore();
  try { if (store) { if (state.accessToken) store.setItem(ACCESS_KEY, state.accessToken); else store.removeItem(ACCESS_KEY); } }
  catch { /* storage refused: this page keeps it in memory only */ }
}
function storedAccess() { const store = accessStore(); try { return (store && store.getItem(ACCESS_KEY)) || ""; } catch { return ""; } }
function accessChannel() { try { return typeof BroadcastChannel === "function" ? new BroadcastChannel(ACCESS_CHANNEL) : null; } catch { return null; } }
function answerAccess(channel) {
  if (channel) channel.addEventListener("message", event => {
    if (event.data && event.data.kind === "need" && state.sessionVerified && state.accessToken)
      channel.postMessage({kind: "access", token: state.accessToken});
  });
}
function askAccess(channel, waitMs = 700) {
  if (!channel) return Promise.resolve("");
  return new Promise(resolve => {
    let timer = null;
    const reply = event => { const data = event.data;
      if (data && data.kind === "access" && typeof data.token === "string" && data.token) done(data.token); };
    const done = token => { channel.removeEventListener("message", reply); clearTimeout(timer); resolve(token); };
    timer = setTimeout(() => done(""), waitMs);
    channel.addEventListener("message", reply);
    channel.postMessage({kind: "need"});
  });
}
// The page route lives in the URL fragment so a reload, Back/Forward or a pasted link
// returns to it: the view, the open case, and on Find & open the search and the open
// record or plan answer. The bearer never does. A person's action adds a history entry
// (push); restoring a route only replaces the current one.
const OBJECT_ROUTE_KEYS = ["q", "asked", "object", "family", "root"];
function routeHash() {
  const params = new URLSearchParams({view: state.view});
  if (state.view === "cases" && state.caseView && state.caseView.case_id) params.set("case", state.caseView.case_id);
  if (state.view === "objects") OBJECT_ROUTE_KEYS.forEach(key => { if (state.objectRoute && state.objectRoute[key]) params.set(key, state.objectRoute[key]); });
  return `#${params}`;
}
function setRoute(push) {
  if (typeof history === "undefined" || typeof location === "undefined") return;
  const hash = routeHash();
  if (location.hash === hash) return;
  const url = `${location.pathname}${location.search}${hash}`;
  if (push) history.pushState(null, "", url); else history.replaceState(null, "", url);
}
// The route in an address fragment: an unknown view reads as home; a case without a view
// opens the cases view; only the known Find & open keys are kept.
function parseRoute(hash) {
  const fragment = new URLSearchParams(String(hash || "").replace(/^#/, ""));
  const caseId = fragment.get("case") || null;
  const named = fragment.get("view");
  const view = labels[named] ? named : (caseId && !named ? "cases" : "home");
  const objectRoute = {};
  if (view === "objects") OBJECT_ROUTE_KEYS.forEach(key => { const v = fragment.get(key); if (v) objectRoute[key] = v; });
  return {view, caseId: view === "cases" ? caseId : null, objectRoute, access: fragment.has("access") ? fragment.get("access") || "" : null};
}
// Times: an instant shows in America/New_York, labelled ET, to the minute; a date-only
// source value names a day and stays as written; a time without a zone says so.
const ET_PARTS = new Intl.DateTimeFormat("en-US", {timeZone: "America/New_York", year: "numeric", month: "short",
  day: "numeric", hour: "numeric", minute: "2-digit", hour12: true});
function etTime(text) {
  const raw = String(text === null || text === undefined ? "" : text).trim();
  if (!raw) return "—";
  const match = /^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2})(?::\d{2}(?:\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?$/i.exec(raw);
  if (!match) return raw;
  if (!match[3]) return `${match[1]} ${match[2]} (time zone not stated)`;
  const at = new Date(raw);
  if (Number.isNaN(at.getTime())) return raw;
  const part = Object.fromEntries(ET_PARTS.formatToParts(at).map(item => [item.type, item.value]));
  return `${part.month} ${part.day}, ${part.year}, ${part.hour}:${part.minute} ${String(part.dayPeriod || "").toUpperCase()} ET`;
}
function timeEl(text) { const node = el("time", "", etTime(text)); if (text) { node.setAttribute("datetime", String(text)); node.title = String(text); } return node; }
// The person's controls (confirm / reject a link, propose a follow-up): JSON POSTs
// carrying the session's CSRF token; the server takes the author from the session.
function post(path, body) { return api(path, {method: "POST", headers: {"Content-Type": "application/json", "X-Operator-Token": state.token}, body: JSON.stringify(body)}); }
async function openPdf(route, filename, page = null) {
  const tab = window.open("about:blank", "_blank");
  if (tab) { tab.opener = null; tab.document.title = "Verifying local PDF"; }
  try {
    const headers = state.accessToken ? {Authorization: `Bearer ${state.accessToken}`} : {};
    const response = await fetch(route, {cache: "no-store", headers});
    if (!response.ok) { const detail = await response.json().catch(() => ({}));
      throw new Error(detail.detail || detail.error || `HTTP ${response.status}`); }
    if (!(response.headers.get("Content-Type") || "").startsWith("application/pdf"))
      throw new Error("Verified original did not return a PDF");
    const blob = await response.blob(), handle = URL.createObjectURL(blob),
      location = Number.isInteger(page) && page >= 1 && page <= 200 ?
        `${handle}#page=${page}` : handle;
    if (tab) tab.location.replace(location);
    else { const link = el("a"); link.href = handle; link.download = filename;
      document.body.appendChild(link); link.click(); link.remove(); }
    setTimeout(() => URL.revokeObjectURL(handle), 120000);
  } catch (error) { if (tab) tab.close(); notice(error.message); }
}
function changeView(view, push, objectRoute) {
  if (!labels[view]) return;
  if (view !== state.view) state.objectRoute = objectRoute || {};
  state.view = view;
  setRoute(push);
  document.querySelectorAll(".nav-item").forEach(b => b.classList.toggle("selected", b.dataset.view === view));
  $("view-crumb").textContent = labels[view][0]; $("page-title").textContent = labels[view][1]; $("page-subtitle").textContent = labels[view][2];
  render();
  if (view === "cases") loadCases();
  if (view === "home") loadHome();
}
function render() {
  const target = clear($("view"));
  if (state.view === "home") homeView(target);
  if (state.view === "objects") {
    if (window.PLXWorkspace) state.objects = window.PLXWorkspace.mount(target, {api, post, route: state.objectRoute,
      onRoute: next => { state.objectRoute = next; setRoute(true); }});
    else target.appendChild(empty("Find & open is unavailable", "workspace.js did not load."));
  }
  if (state.view === "cases") casesView(target);
}
function refresh() {
  notice("");
  $("updated-at").textContent = `REFRESHED ${etTime(new Date().toISOString())}`;
  if (state.view === "cases") refreshCases();
  if (state.view === "home") loadHome();
}

function openDetail(content) {
  const holder = $("detail-body"), previous = holder.firstElementChild;
  if (previous && !state.detailRestoring &&
      (!content.dataset.detailKey || content.dataset.detailKey !== previous.dataset.detailKey)) {
    state.detailTrail.push(previous);
    if (state.detailTrail.length > 12) state.detailTrail.shift();
  }
  clear(holder).appendChild(content);
  $("detail").classList.remove("hidden");
  // Back is always offered on an open detail: to the previous view, or, from the first
  // one, back to the list underneath (the detail closes).
  $("back-detail").hidden = false;
}
async function backDetail() {
  const previous = state.detailTrail.pop();
  if (!previous) { closeDetail(); return; }
  state.detailRestoring = true;
  try { openDetail(previous); }
  finally { state.detailRestoring = false; }
  try { if (typeof previous._refresh === "function") await previous._refresh(); }
  catch (error) { notice(`Previous view restored, but current source refresh failed: ${error.message}`); }
}
function closeDetail() {
  state.detailTrail = [];
  $("back-detail").hidden = true;
  $("detail").classList.add("hidden"); clear($("detail-body"));
}
function detailSection(name, data) { const n = el("section", "detail-section"); append(n, el("h3", "", name), fields(data)); return n; }

// ---- Case workspace (read-only; devloop/CASE-RUNTIME.md section 3) ----------------
// Pure helpers first (tested by operator_ui/case_workspace.test.js), then rendering.
const CASE_FINDING_GROUPS = [
  ["contradicted", "Contradicted", "red", "A cited source disagrees with the claim."],
  ["unknown", "Unknown", "amber", "Not established from the cited sources."],
  ["citation_content_changed", "Source changed since cited", "amber", "A cited source now reads differently; the recorded claim is not vouched for."],
  ["withheld", "Withheld", "red", "A citation does not resolve for you now; content is not shown."],
  ["supported", "Supported", "", "Backed by every cited source."],
  ["superseded", "Superseded", "blue", "Replaced by a later finding."]];
const CASE_GAP_STATES = {unknown: "Unknown", absent_in_source: "Absent in source", inaccessible: "Inaccessible", stale: "Stale", not_ingested: "Not ingested"};
function caseApiKey(key) { return key && typeof key === "object" ? Object.entries(key).map(([name, val]) => `${name}=${val}`).join(", ") : value(key); }
// Who this console session is, from /api/session's identity: a live employee (signed
// ticket) or a fixture session, never an unnamed default.
function sessionLabel(identity) {
  if (!identity) return "No session identity";
  if (identity.fixture) return "Fixture session · not a signed-in employee";
  const employee = String(identity.employee || "");
  const until = Number.isInteger(identity.expires_at)
    ? ` · until ${etTime(new Date(identity.expires_at * 1000).toISOString())}` : "";
  return `Signed in: ${employee ? "employee …" + employee.slice(-4) : "unknown employee"} · ${identity.author_kind || "unknown"}${until}`;
}

// The session's role: the audience principals the server resolved for it (``staff:operations``
// reads "Operations staff"), never a guess.
function sessionRole(principals) {
  const names = array(principals).map(String).filter(Boolean).map(principal => {
    const [kind, ...rest] = principal.split(":"), what = rest.join(":").replace(/[_-]+/g, " ").trim();
    // A live employee's role is what their employee access grants now, checked on every request.
    if (kind === "employee") return "your employee access, checked on every request";
    if (!what) return principal;
    return `${what.charAt(0).toUpperCase()}${what.slice(1)} ${kind === "staff" ? "staff" : `(${kind})`}`;
  });
  return principals === null || principals === undefined ? "Role: not stated by this console"
    : names.length ? `Role: ${names.join(", ")}` : "Role: none granted";
}

function citationLabel(handle) {
  const text = String(handle || "");
  const api = /^api:\/\/([^/]+)\/([^@]+)@/.exec(text);
  if (api) { let key = api[2]; try { key = caseApiKey(JSON.parse(decodeURIComponent(api[2]))); } catch { /* keep raw */ } return `${api[1]} · ${key}`; }
  const original = /^original:\/\/([^#]+)(?:#(.*))?$/.exec(text);
  if (original) { const params = new URLSearchParams(original[2] || ""); const page = params.get("page"), version = params.get("extraction");
    return `original ${original[1].slice(0, 12)}${version ? ` v${version}` : ""}${page ? ` · page ${page}` : ""}`; }
  return text || "citation";
}
function caseFindingGroups(view) {
  const findings = (view && view.findings) || {};
  return CASE_FINDING_GROUPS.map(([status, label, tone, hint]) => ({status, label, tone, hint, items: array(findings[status])}))
    .filter(group => group.items.length);
}
function caseGapSummary(view) {
  const gaps = array(view && view.gaps), findings = (view && view.findings) || {};
  const visible = gaps.filter(gap => gap.content !== "withheld_citation_unavailable");
  const byState = {};
  visible.forEach(gap => { byState[gap.state] = (byState[gap.state] || 0) + 1; });
  return {gaps: visible, withheld_gaps: gaps.length - visible.length, by_state: byState,
    unknown: (byState.unknown || 0) + array(findings.unknown).length,
    contradicted: array(findings.contradicted).length,
    changed: array(findings.citation_content_changed).length,
    withheld: array(findings.withheld).length + gaps.length - visible.length +
      [...array(view && view.calculations), ...array(view && view.proposals)].filter(item => item.content === "withheld_citation_unavailable").length};
}
// Case events a person acts on; every other event (a finding or gap recorded) is the
// investigation's bookkeeping, listed collapsed under the business events.
const CASE_EVENT_WORDS = {"case.opened": "Case opened", "case.closed": "Case closed", "case.reopened": "Case reopened",
  "proposal.recorded": "Action proposed (needs approval)", "verification.recorded": "Action outcome read back"};
function caseTimeline(view) {
  // A case event names the item it recorded; that item's resolved citations go with it.
  const cites = new Map(caseCitingItems(view).map(item => [item.id, item.citations.filter(cite => cite && cite.status === "resolved" && cite.handle).map(cite => cite.handle)]));
  const events = array(view && view.timeline).map(event => ({at: String(event.at || ""), source: "case",
    label: `${String(event.kind || "event").replace(/\./g, " ")}${event.ref ? ` · ${event.ref}` : ""}`,
    plain: CASE_EVENT_WORDS[event.kind] || null, business: Boolean(CASE_EVENT_WORDS[event.kind]),
    detail: `case event #${value(event.seq)} by ${value(event.actor)}`, ref: event.ref || null,
    handles: [...new Set(cites.get(event.ref) || [])]}));
  const dates = array(view && view.source_dates).map(item => ({at: String(item.at || ""), source: "source",
    label: `${item.relation} · ${item.column}`, plain: item.label || null, business: true, detail: item.relation === "original" ? `original ${value(item.key)} (source item date)` :
      item.relation === "mail" ? `mail message ${value(item.key)} (as the mailbox records it)` :
      item.relation === "bc" ? `Business Central record ${value(item.key)}` : `cited row ${caseApiKey(item.key)} (source value, as recorded upstream)`,
    handle: item.handle}));
  return events.concat(dates).map((entry, index) => ({...entry, index}))
    .sort((a, b) => a.at < b.at ? -1 : a.at > b.at ? 1 : a.index - b.index)
    .map(({index, ...entry}) => entry);
}
function caseCitingItems(view) {
  const items = [];
  const findings = (view && view.findings) || {};
  Object.entries(findings).forEach(([bucket, list]) => array(list).forEach(item =>
    items.push({kind: "finding", id: item.id, status: item.status || bucket, text: item.statement, citations: array(item.citations)})));
  array(view && view.calculations).forEach(calc => items.push({kind: "calculation", id: calc.id,
    status: calc.inputs_status || (calc.content ? "withheld" : ""), text: calc.expression ? `${calc.expression} = ${value(calc.result)} ${value(calc.unit)}` : null,
    citations: Object.values(calc.inputs || {}).filter(spec => spec && spec.kind === "citation").map(spec => spec.citation || {})}));
  array(view && view.gaps).forEach(gap => items.push({kind: "gap", id: gap.id, status: gap.state, text: gap.missing, citations: array(gap.citations)}));
  array(view && view.proposals).forEach(item => items.push({kind: "proposal", id: item.id, status: "needs human approval", text: item.capability, citations: array(item.citations)}));
  return items;
}
function caseRelationships(view) {
  const nodes = new Map();
  array(view && view.records).forEach(record => nodes.set(record.handle, {handle: record.handle, label: `${record.relation} · ${caseApiKey(record.key)}`, kind: "record"}));
  array(view && view.originals).forEach(original => array(original.cited_as).forEach(handle =>
    nodes.set(handle, {handle, label: `${value(original.filename)}${/page=(\d+)/.test(handle) ? ` · page ${/page=(\d+)/.exec(handle)[1]}` : ""}`, kind: "original"})));
  const links = [], neighbours = new Map([...nodes.keys()].map(handle => [handle, []]));
  caseCitingItems(view).forEach(item => {
    const handles = [...new Set(item.citations.filter(cite => cite.status === "resolved" && nodes.has(cite.handle)).map(cite => cite.handle))];
    if (handles.length < 2) return;
    const via = {kind: item.kind, id: item.id, status: item.status, text: item.text};
    links.push({via, handles});
    handles.forEach(handle => handles.filter(other => other !== handle).forEach(other =>
      neighbours.get(handle).push({handle: other, via})));
  });
  return {nodes: [...nodes.values()], links, neighbours};
}
function caseEvaluationSummary(evaluation) {
  const e = evaluation || {};
  if (e.status === "not_configured") return {tone: "blue", headline: "No evaluation store configured on this host", lines: []};
  if (e.status === "none") return {tone: "blue", headline: "Not evaluated yet", lines: ["No stored playbook evaluation for this case."]};
  if (e.status === "withheld") return {tone: "amber", headline: "Evaluation withheld", lines: ["Part of this case is withheld from you or changed since cited, so the stored evaluation is not shown."]};
  if (e.status === "integrity_error") return {tone: "red", headline: "Stored evaluation failed its integrity check", lines: [value(e.detail)]};
  if (e.status !== "stored") return {tone: "blue", headline: "Evaluation unavailable", lines: []};
  // The workspace never sends the recorder (its label carries principals); say so, not "—".
  const recorder = typeof e.recorded_by === "string" && e.recorded_by.trim() ? e.recorded_by : "a recorder not shown here";
  const limits = array(e.limits), lines = [`${value(e.passed)} of ${value(e.checks)} checks passed`,
    `playbook ${value(e.playbook_id)} v${value(e.playbook_version)} · evaluator ${value(e.evaluator_version)}`,
    `recorded ${etTime(e.recorded_at)} by ${recorder} · transcript ${value(e.transcript_source)}`];
  // A report scored the case at its event boundary; a later event makes it stale.
  const stale = e.freshness === "stale", unknownFreshness = e.freshness !== "current";
  if (stale) lines.unshift(`Stale: ${e.freshness_reason || "the case changed after this evaluation"}. Re-run the evaluation before relying on it.`);
  else if (unknownFreshness) lines.unshift(`Freshness unknown: ${e.freshness_reason || "the report does not say which case events it read"}.`);
  if (array(e.failed_ids).length) lines.push(`Failed: ${e.failed_ids.join(", ")}`);
  if (limits.length) lines.push(`Limits: ${limits.join(", ")}`);
  if (e.passed_all && !e.passed_all_clean) lines.push(`Not clean: ${[e.passed_all_qualification, ...limits].filter(Boolean).join(", ") || "qualified"}`);
  const result = e.passed_all_clean ? "Passed all checks (clean)" : e.passed_all ? "Passed all checks, not clean" : `${value(e.failed)} check(s) failed`;
  const headline = stale ? `Stale evaluation: ${result}` : unknownFreshness ? `Freshness unknown: ${result}` : result;
  const tone = e.passed_all ? (e.passed_all_clean && !unknownFreshness ? "" : "amber") : "red";
  return {tone, headline, lines, stale};
}
// ---- Case view (one view: evidence label, conflicts, relationships, actions) ---------
const CASE_GAP_ORDER = ["unknown", "inaccessible", "absent_in_source", "stale", "not_ingested"];
function caseEvidenceBanner(view) {
  const e = (view && view.evidence) || {};
  if (e.fixture) return {fixture: true, tone: "red", headline: `FIXTURE DATA · ${e.label || "fixture"} · not real PLX data`,
    note: e.note || "This state directory was written from fixtures, not from PLX systems."};
  return {fixture: false, tone: "blue", headline: `Evidence: ${e.label || "recorded case state"}`,
    note: e.note || "The host did not state where this data came from; every citation is re-read for you."};
}
function caseConflictGapIds(view) {
  return new Set(array(view && view.conflicts).filter(item => item.kind === "evidence_conflict").map(item => item.id));
}
// The same gap recorded once per copy of a record (same state, words and who can answer)
// reads as one line: its ids kept, its citations merged, its copy count shown.
function collapseCaseGaps(gaps) {
  const kept = new Map();
  gaps.forEach(gap => {
    const words = gapWords(gap), key = JSON.stringify([gap.state, words.title, words.text || "", words.who]);
    const seen = kept.get(key);
    if (!seen) { kept.set(key, {...gap, ids: [gap.id], copies: 1, citations: array(gap.citations).slice()}); return; }
    seen.ids.push(gap.id); seen.copies += 1;
    array(gap.citations).forEach(cite => { if (!seen.citations.some(other => other.handle === cite.handle)) seen.citations.push(cite); });
  });
  return [...kept.values()];
}
function caseGapGroups(view) {
  const conflictIds = caseConflictGapIds(view);
  const visible = array(view && view.gaps).filter(gap => gap.content !== "withheld_citation_unavailable" && !conflictIds.has(gap.id));
  const states = [...new Set([...CASE_GAP_ORDER, ...visible.map(gap => gap.state)])];
  return states.map(state => ({state, label: CASE_GAP_STATES[state] || String(state || "unknown"),
    gaps: collapseCaseGaps(visible.filter(gap => gap.state === state))})).filter(group => group.gaps.length);
}
function caseApprovalSummary(approval) {
  const a = approval || {};
  if (a.state === "unknown_scan_truncated") return {tone: "amber", headline: "Approval state unknown · action store scan bounded", lines: [value(a.detail), "Dispatched: unknown"], dispatched: null};
  if (a.state === "unknown_list_truncated") return {tone: "amber", headline: "Approval state unknown · action listing bounded", lines: [value(a.detail), "Dispatched: unknown"], dispatched: null};
  if (a.state === "unknown_chain_broken") return {tone: "red", headline: "Approval state unknown · an action record fails its hash chain", lines: [value(a.detail), "Dispatched: unknown"], dispatched: null};
  if (a.state === "action_store_unreadable") return {tone: "red", headline: "Action store unreadable", lines: [value(a.detail)], dispatched: null};
  if (a.withheld) return {tone: "amber", headline: `Withheld proposal · action path state ${value(a.state)}`,
    lines: ["What it proposes is withheld from you: a citation does not resolve for you now.", `Dispatched: ${a.dispatched === null || a.dispatched === undefined ? "unknown" : a.dispatched ? "yes" : "no"}`],
    dispatched: a.dispatched === null || a.dispatched === undefined ? null : Boolean(a.dispatched)};
  if (!a.registered) return {tone: "amber", headline: "Not requested · needs human approval in Mission Control",
    lines: [a.path || "Not on the goal-2 action path: no Mission Control approval has been requested.", "Dispatched: no"], dispatched: false};
  const lines = [`Action path state: ${value(a.state)} (intent ${value(a.intent_id)}, owner ${value(a.owner_system)})`,
    `Dispatched: ${a.dispatched ? "yes" : "no"}${a.dispatch_attempts ? ` (${a.dispatch_attempts} attempt(s))` : ""}`];
  if (a.gate) lines.push(`Mission Control gate: task ${value(a.gate.task_id)}${a.gate.gate_id ? `, gate ${a.gate.gate_id}` : ", no gate id yet"}`);
  else lines.push("Mission Control gate: not requested yet");
  array(a.decisions).forEach(d => lines.push(`MC decision: ${value(d.status)}${d.authorizes ? " (authorizes)" : " (does not authorize)"}${d.decided_at ? ` at ${etTime(d.decided_at)}` : ""}`));
  if (array(a.human_preconditions).length) lines.push(`A person confirms: ${a.human_preconditions.join("; ")}`);
  if (a.checked_preconditions) lines.push(`${a.checked_preconditions} precondition(s) checked on the cited evidence at registration`);
  array(a.read_backs).forEach(outcome => lines.push(`Read-back: ${outcome}`));
  if (a.next_step) lines.push(`Next safe step: ${a.next_step}`);
  else if (a.next_safe_step && a.next_safe_step.detail) lines.push(`Next safe step${a.next_safe_step.by ? ` (${a.next_safe_step.by})` : ""}: ${a.next_safe_step.detail}`);
  if (a.chain_ok === false) lines.push("Action record hash chain is broken.");
  const authorized = a.state === "gate_authorized", done = a.state === "verified";
  const tone = a.chain_ok === false || a.state === "dispatched_unknown" || a.state === "escalated" ? "red" : done ? "" : authorized ? "blue" : "amber";
  const headline = done ? "Done: effect verified by read-back" : a.dispatched ? `Dispatched after MC approval · ${a.state}` :
    authorized ? "Approved in Mission Control · not dispatched" : `Awaiting human approval in Mission Control · ${a.state}`;
  return {tone, headline, lines, dispatched: Boolean(a.dispatched)};
}
function caseOriginalStatus(original) {
  const o = original || {}, source = o.source || {}, integrity = o.integrity || {};
  const sha = {tone: integrity.status === "current" ? "" : "amber",
    text: `SHA-256 ${String(o.sha256 || "").slice(0, 16)}… re-hashed on this read${integrity.status && integrity.status !== "current" ? ` (${integrity.status})` : ""}`};
  let etag;
  if (source.kind === "sharepoint-drive") {
    const tones = {current: "", changed_since: "amber", not_rechecked: "blue", unavailable: "red"};
    const words = {current: "eTag unchanged since attach", changed_since: "changed in SharePoint since attach (stored bytes are the attached version)",
      not_rechecked: "eTag not rechecked on this host", unavailable: "SharePoint item not readable for you now"};
    etag = {tone: tones[source.etag_status] ?? "amber", text: `SharePoint ${value(source.path)} · ${words[source.etag_status] || value(source.etag_status)}`,
      detail: source.etag_reason || ""};
  } else etag = {tone: "blue", text: "Operator inbox original (no source item to recheck)", detail: ""};
  return {sha, etag};
}
function caseCitationChip(cite) {
  if (!cite || !cite.handle) { const span = el("span", "case-chip withheld", `citation ${value(cite && cite.index)} unavailable`); span.title = "This citation does not resolve for you now."; return span; }
  const chip = el("button", `case-chip${cite.status === "resolved" ? "" : " changed"}`, citationLabel(cite.handle));
  chip.type = "button"; chip.title = cite.handle;
  chip.setAttribute("aria-label", `Open cited source ${citationLabel(cite.handle)}${cite.status === "resolved" ? "" : " (changed since cited)"}`);
  chip.addEventListener("click", () => openCaseCitation(cite.handle));
  return chip;
}
function caseChips(cites) { const box = el("div", "case-chips"); array(cites).forEach(cite => box.appendChild(caseCitationChip(cite))); return box; }
function caseEvidenceElement(view) {
  const banner = caseEvidenceBanner(view);
  const box = el("div", `case-evidence ${banner.fixture ? "fixture" : ""}`.trim());
  box.setAttribute("role", banner.fixture ? "alert" : "note");
  append(box, el("strong", "", banner.headline), el("span", "", banner.note));
  return box;
}
function caseCoveragePanel(coverage) {
  if (!coverage || coverage.status === "not_configured") return null;
  const body = el("div", "stack");
  if (coverage.status !== "read") { body.appendChild(el("p", "callout", `Ingestion status not shown: ${value(coverage.error || coverage.status)}`)); return panel("Source coverage", "PLX.INGEST_STATUS", body); }
  const summary = coverage.summary || {};
  body.appendChild(fields({units: summary.units, complete: summary.complete, incomplete: summary.incomplete,
    interrupted: summary.interrupted, never_run: summary.never_run, withheld_units: (coverage.visibility || {}).units_withheld}));
  array(coverage.units).forEach(unit => body.appendChild(el("small", "text-muted",
    `${value(unit.kind)} ${value(unit.source_id)}: ${value(unit.state)}${array(unit.reasons).length ? ` (${unit.reasons.join(", ")})` : ""}`)));
  array(coverage.caveats).forEach(line => body.appendChild(el("small", "text-muted", line)));
  return panel("Source coverage", "PLX.INGEST_STATUS · COUNTS ONLY", body);
}
async function openCaseDocumentMetadata(handle, filename) {
  try {
    const data = await api(`/api/case-document-metadata?handle=${encodeURIComponent(handle)}`);
    const body = el("div", "detail-content"); body.dataset.detailKey = `metadata:${handle}`;
    append(body, tag("proposals only"), el("h2", "", `Document metadata · ${value(filename)}`),
      el("p", "callout", `Portal-taxonomy proposals (${value((data.portal_taxonomy || {}).columns)}); reading: ${value(data.reading)}. Nothing is saved or written to SharePoint.`));
    const anchor = data.anchor || {};
    body.appendChild(detailSection("Document is about", {status: anchor.status, entity_type: anchor.entity_type}));
    array(data.linked_records).forEach(link => body.appendChild(detailSection(`Record named in the text: ${value(link.link)}`,
      {values: array(link.values).join(", "), record_status: link.record_status, requires_review: link.requires_review})));
    const proposed = array(data.columns).filter(column => column.status === "proposed" || column.status === "conflict" || column.status === "model_suggestion");
    proposed.forEach(column => body.appendChild(detailSection(column.display_name || column.column,
      {status: column.status, value: column.value, basis: column.basis, requires_review: column.requires_review, portal_rule: column.portal_rule})));
    if (!proposed.length) body.appendChild(el("p", "text-muted", "No column value is proposed from this document's text."));
    openDetail(body);
  } catch (error) { notice(error.message); }
}
// A recorded gap in employee words (``plain``, from the server: operator_ui/case_plain.py)
// with what was recorded kept, as recorded, in an engineer's detail. A gap without
// ``plain`` shows as recorded.
function gapWords(gap) {
  const plainWords = gap && gap.plain;
  if (!plainWords || !plainWords.title) return {title: value(gap && (gap.missing || gap.question)), text: gap && gap.why,
    who: array(gap && gap.who_can_answer), engineer: null};
  return {title: plainWords.title, text: plainWords.text, who: array(plainWords.who), engineer: gap};
}
function engineerDetail(gap) {
  const box = el("details", "engineer");
  append(box, el("summary", "", "Engineer detail"),
    el("code", "", `id ${value(gap.id)}`), el("code", "", `missing: ${value(gap.missing || gap.question)}`),
    gap.why ? el("code", "", `why: ${gap.why}`) : null,
    array(gap.who_can_answer).length ? el("code", "", `who_can_answer: ${gap.who_can_answer.join(", ")}`) : null);
  return box;
}
function gapCard(gap, cls, tags) {
  const words = gapWords(gap), card = el("article", cls);
  append(card, append(el("div", "case-tags"), ...tags, words.engineer ? null : el("small", "text-muted", value(gap.id))),
    el("strong", "", words.title));
  if (words.text) card.appendChild(el("p", "", words.text));
  card.appendChild(el("small", "text-muted", `Who can answer: ${words.who.join(" or ") || "—"}`));
  return {card, words};
}
// The case title and business facts (``summary``, from the cited rows this reader may read
// now), never the question's plan names run into the case id.
const CASE_ROOT_TAIL = /:\s*([A-Z][A-Za-z]+)\s+(\S+)\s*$/;
function typeWords(typeId) { const text = String(typeId || "").replace(/([a-z])([A-Z])/g, "$1 $2").toLowerCase(); return text.charAt(0).toUpperCase() + text.slice(1); }
function caseQuestions(question) { return String(question || "").replace(CASE_ROOT_TAIL, "").split("; ").map(part => part.trim()).filter(Boolean); }
function caseTitle(item) {
  const summary = item && item.summary;
  if (summary && summary.title && summary.title !== "Case") return summary.title;
  const tail = CASE_ROOT_TAIL.exec(String(item && item.question || ""));
  if (tail) return `${typeWords(tail[1])} ${tail[2]}`;
  const roots = array(item && item.roots);
  if (roots.length) return `Case about ${roots.join(", ")}`;
  const questions = caseQuestions(item && item.question);
  return questions.length ? `${questions[0]}${questions.length > 1 ? ` (+${questions.length - 1} more)` : ""}` : "Case";
}
function caseFacts(view) {
  const summary = view.summary || {}, facts = el("dl", "case-facts"), rows = [];
  array(summary.roots).forEach(root => rows.push([root.type_label || "Record", `${value(root.label || root.id)}${root.status ? ` · status ${root.status}` : ""}`]));
  const party = summary.counterparty;
  if (party) rows.push([party.type_label || "Counterparty", party.name ? `${party.name}${party.tier_meaning ? ` · ${party.tier_meaning}` : ""}${party.standing ? ` · standing ${party.standing}` : ""}`
    : `${value(party.reference)}${party.note ? ` (${party.note})` : ""}`]);
  else if (summary.roots) rows.push(["Counterparty", "Not named on the records you may read"]);
  array(summary.lines).forEach(line => rows.push([typeWords(line.kind), `${value(line.cited)} cited`]));
  rows.push(["Open gaps", value(summary.open_gaps !== undefined ? summary.open_gaps : caseGapSummary(view).gaps.length)]);
  rows.push(["Case", `${value(view.status)}${view.outcome ? ` · outcome: ${view.outcome}` : ""}`]);
  rows.forEach(([name, text]) => append(facts, el("dt", "", name), el("dd", "", text)));
  return facts;
}
// A case root's status card (/api/status-card by its type and reference), rendered by the
// object workspace's renderer; a card that did not read says why in one line.
function caseStatusCards(view) {
  const render = typeof PLXWorkspace !== "undefined" && PLXWorkspace.renderStatusCard;
  const cards = array(view.status_cards);
  if (!render || !cards.length) return null;
  const box = el("div", "case-status-cards");
  cards.forEach(card => box.appendChild(render(document, card, {openable: true,
    citationNode: cite => caseCitationChip({handle: cite.handle, status: "resolved"})})));
  return box;
}
async function loadCaseStatusCards(view) {
  const roots = array(view && view.summary && view.summary.roots).filter(root => root.type && (root.label || root.id));
  return Promise.all(roots.map(root => api(`/api/status-card?type=${encodeURIComponent(root.type)}&ref=${encodeURIComponent(root.label || root.id)}`)
    .catch(() => null))).then(cards => cards.filter(Boolean));   // not served: no card, the case still opens
}
function renderCaseWorkspace(view) {
  const root = el("div", "case-workspace"); root.dataset.caseId = value(view.case_id);
  // The status card first: what is wrong, why and what to do next.
  const cards = caseStatusCards(view);
  if (cards) root.appendChild(cards);
  root.appendChild(caseEvidenceElement(view));
  const header = el("section", "panel case-header"), head = el("div", "panel-body");
  const questions = caseQuestions(view.question), asked = el("details", "case-asked");
  append(asked, el("summary", "", `${questions.length} question${questions.length === 1 ? "" : "s"} asked`),
    append(el("ul", ""), ...questions.map(question => el("li", "", question))));
  const meta = el("small", "text-muted case-meta");
  append(meta, el("span", "", `${value(view.case_id)} · opened `), timeEl(view.opened_at), el("span", "", " · updated "), timeEl(view.updated_at));
  append(head, append(el("div", "case-tags"), tag(view.status), tag(view.integrity && view.integrity.chain_ok ? "hash chain ok" : "hash chain broken"), tag("read only")),
    el("h2", "case-question", caseTitle(view)), caseFacts(view), asked,
    append(el("div", "case-roots"), el("span", "text-muted", "Roots: "), ...array(view.roots).map(rootId => el("span", "case-root", rootId))),
    meta);
  header.appendChild(head); root.appendChild(header);
  const gaps = caseGapSummary(view), conflictsList = array(view.conflicts);
  const attention = el("div", "case-attention"); attention.setAttribute("role", "status");
  append(attention, el("span", `case-count${conflictsList.length ? " red" : ""}`, `${conflictsList.length} conflicts`),
    el("span", `case-count${gaps.contradicted ? " red" : ""}`, `${gaps.contradicted} contradicted`),
    el("span", `case-count${gaps.unknown ? " amber" : ""}`, `${gaps.unknown} unknown`),
    el("span", "case-count", `${gaps.gaps.length} gaps`),
    el("span", `case-count${gaps.withheld ? " red" : ""}`, `${gaps.withheld} withheld`),
    el("span", `case-count${gaps.changed ? " amber" : ""}`, `${gaps.changed} source changed`));
  root.appendChild(attention);
  const truncated = Object.entries(view.truncated || {});
  if (truncated.length) root.appendChild(el("p", "callout", `Large case: showing ${truncated.map(([name, count]) => `${count.shown} of ${count.total} ${name.replace(/_/g, " ")}`).join(", ")}. The rest is not shown here.`));
  const grid = el("div", "case-grid");
  // Conflicts and unknowns first: they must be visible, not buried.
  if (view.conflicts !== undefined) {
    const conflictBody = el("div", "stack");
    if (!conflictsList.length) conflictBody.appendChild(empty("No conflicts visible", "No recorded evidence disagrees, as far as you can read this case."));
    conflictsList.forEach(item => { const {card, words} = gapCard(item, "case-gap case-conflict",
        [el("span", "tag red", String(item.kind || "conflict").replace(/_/g, " "))]);
      array(item.sides).forEach(side => { const row = el("div", "case-conflict-side");
        append(row, el("small", "text-muted", side.side), caseChips(array(side.citations).filter(Boolean).map(handle => ({handle, status: "resolved"}))));
        card.appendChild(row); });
      if (words.engineer) card.appendChild(engineerDetail(item));
      conflictBody.appendChild(card); });
    grid.appendChild(panel("Conflicts", "EVIDENCE DISAGREES · A PERSON DECIDES", conflictBody));
  }
  const gapBody = el("div", "stack"), groups = caseGapGroups(view);
  if (!groups.length && !gaps.withheld_gaps) gapBody.appendChild(empty("No gaps recorded", "Nothing has been recorded as missing for this case."));
  groups.forEach(group => { const section = el("details", "case-gap-group"); section.dataset.state = group.state;
    if (group.state === "unknown" || group.gaps.length <= 6) section.open = true;
    section.appendChild(el("summary", "", `${group.label} · ${group.gaps.length}`));
    group.gaps.forEach(gap => { const {card, words} = gapCard(gap, `case-gap ${gap.state === "unknown" ? "amber" : ""}`.trim(),
        [tag(CASE_GAP_STATES[gap.state] || gap.state), gap.copies > 1 ? tag(`${gap.copies} copies`) : null].filter(Boolean));
      card.appendChild(caseChips(gap.citations));
      if (words.engineer) card.appendChild(engineerDetail(gap));
      section.appendChild(card); });
    gapBody.appendChild(section); });
  if (gaps.withheld_gaps) gapBody.appendChild(el("p", "callout", `${gaps.withheld_gaps} gap(s) withheld: a citation does not resolve for you.`));
  grid.appendChild(panel("Gaps", "UNKNOWN · MISSING · NOT INGESTED", gapBody));
  const rel = caseRelationships(view), relBody = el("div", "case-relations");
  if (!rel.nodes.length) relBody.appendChild(empty("No cited records visible", "Records appear here when a citation resolves for you."));
  else { const table = el("table", "case-relation-table"); const thead = el("thead"), headRow = el("tr");
    ["Cited record", "Linked to", "Declared by"].forEach(name => { const th = el("th", "", name); th.setAttribute("scope", "col"); headRow.appendChild(th); });
    append(table, append(thead, headRow)); const tbody = el("tbody");
    rel.nodes.forEach(node => { const tr = el("tr"), links = rel.neighbours.get(node.handle) || [];
      const cell = el("td"); cell.appendChild(caseCitationChip({handle: node.handle, status: "resolved"}));
      const linked = el("td"), declared = el("td");
      if (!links.length) linked.appendChild(el("span", "text-muted", "no co-cited record"));
      const seen = new Set();
      links.forEach(link => { if (!seen.has(link.handle)) { seen.add(link.handle); linked.appendChild(el("div", "", rel.nodes.find(other => other.handle === link.handle).label)); } });
      [...new Map(links.map(link => [`${link.via.kind}:${link.via.id}`, link.via])).values()].forEach(via =>
        declared.appendChild(el("div", "", `${via.kind} ${value(via.id)} (${value(via.status)})`)));
      append(tr, cell, linked, declared); tbody.appendChild(tr); });
    append(table, tbody); relBody.appendChild(append(el("div", "table-scroll"), table));
    relBody.appendChild(el("small", "text-muted", "A link means one finding, calculation, gap or proposal cites both records; it is what that item declares, not a verified foreign key. Typed relationships and their identity tiers are in the object workspace.")); }
  grid.appendChild(panel("Relationships", `${rel.nodes.length} RECORDS · ${rel.links.length} LINKS`, relBody));
  const timelineBody = el("div", "stack"), entries = caseTimeline(view);
  const timeItem = entry => { const item = el("li", `case-time ${entry.source}`), named = el("strong", "", entry.plain || entry.label);
    if (entry.plain) named.title = entry.label;
    append(item, timeEl(entry.at), el("span", "case-time-source", entry.source === "case" ? "case" : "source"),
      named, el("small", "text-muted", entry.detail));
    if (entry.handle) item.appendChild(caseCitationChip({handle: entry.handle, status: "resolved"}));
    if (array(entry.handles).length) item.appendChild(caseChips(entry.handles.map(handle => ({handle, status: "resolved"}))));
    return item; };
  const business = entries.filter(entry => entry.business), bookkeeping = entries.filter(entry => !entry.business);
  timelineBody.appendChild(business.length ? append(el("ol", "case-timeline"), ...business.map(timeItem))
    : empty("No business events dated", "No cited record carries a date you may read."));
  if (bookkeeping.length) { const kept = el("details", "case-bookkeeping");
    append(kept, el("summary", "", `Investigation bookkeeping · ${bookkeeping.length} entries (findings and gaps recorded)`),
      append(el("ol", "case-timeline"), ...bookkeeping.map(timeItem)));
    timelineBody.appendChild(kept); }
  grid.appendChild(panel("Timeline", "BUSINESS EVENTS FIRST · TIMES IN ET", timelineBody));
  const origBody = el("div", "stack"), originals = array(view.originals);
  if (!originals.length) origBody.appendChild(empty("No originals visible", "No attached original is cited on this case, or none is in your audience."));
  originals.forEach(original => { const card = el("article", "case-original"), extraction = original.extraction || {}, status = caseOriginalStatus(original);
    append(card, el("strong", "", value(original.filename)),
      el("small", "text-muted", `sha256 ${String(original.sha256 || "").slice(0, 16)}… · ${value(original.bytes)} bytes · extraction ${value(extraction.status)} (${value(extraction.method)})`));
    if (original.source) { append(card, append(el("div", "case-tags"), el("span", `tag ${status.sha.tone}`.trim(), status.sha.text)),
      append(el("div", "case-tags"), el("span", `tag ${status.etag.tone}`.trim(), status.etag.text)));
      if (status.etag.detail) card.appendChild(el("small", "text-muted", status.etag.detail)); }
    const pages = el("div", "case-chips");
    array(original.pages).forEach(page => { const button = el("button", "case-chip", `Page ${value(page.page)}${page.text_source === "ocr" ? " · OCR" : ""}`);
      button.type = "button"; button.setAttribute("aria-label", `Read page ${value(page.page)} text of ${value(original.filename)}${page.text_source === "ocr" ? " (machine-read OCR)" : ""}`);
      button.addEventListener("click", () => openCaseCitation(page.citation)); pages.appendChild(button); });
    const file = el("button", "case-chip", "File summary"); file.type = "button"; file.addEventListener("click", () => openCaseCitation(original.citation));
    const pdf = el("button", "case-chip", "Open PDF"); pdf.type = "button";
    pdf.setAttribute("aria-label", `Open the stored PDF of ${value(original.filename)}`);
    pdf.addEventListener("click", () => openPdf(`/api/case-original-pdf?handle=${encodeURIComponent(original.citation)}`, "case-original.pdf"));
    const meta = el("button", "case-chip", "Metadata proposals"); meta.type = "button";
    meta.setAttribute("aria-label", `Show Portal metadata proposals for ${value(original.filename)} (proposals only)`);
    meta.addEventListener("click", () => openCaseDocumentMetadata(original.citation, original.filename));
    append(card, append(pages, file, pdf, meta));
    if (original.pages_truncated) card.appendChild(el("small", "text-muted", `Showing ${array(original.pages).length} of ${value(original.pages_total)} pages.`));
    if (array(extraction.ocr_pages).length) card.appendChild(el("small", "text-muted", "OCR pages are machine-read: what the scan appears to say, for a person to confirm."));
    origBody.appendChild(card); });
  grid.appendChild(panel("Originals", `${originals.length} ATTACHED · SHA-256 + ETAG`, origBody));
  const propBody = el("div", "stack"), proposals = array(view.proposals);
  if (!proposals.length) propBody.appendChild(empty("No proposals", "Nothing has been proposed on this case."));
  proposals.forEach(item => { const card = el("article", "case-proposal"), approval = caseApprovalSummary(item.approval);
    append(card, append(el("div", "case-tags"), el("span", `tag ${approval.tone}`.trim(), approval.headline), el("small", "text-muted", value(item.id))),
      el("strong", "", item.capability === null || item.capability === undefined ? "Proposal withheld: a citation does not resolve for you now." : item.capability));
    if (item.payload) card.appendChild(fields(item.payload));
    if (array(item.preconditions).length) card.appendChild(el("p", "text-muted", `Preconditions: ${item.preconditions.map(c => typeof c === "string" ? c : `${value(c.check)} ${value(c.field)}`).join("; ")}`));
    approval.lines.forEach(line => card.appendChild(el("small", "text-muted", line)));
    card.appendChild(el("p", "callout", (item.approval && item.approval.note) || "Recorded only. Needs human approval in Mission Control."));
    array(item.verifications).forEach(check => card.appendChild(el("small", "text-muted", `Read-back ${value(check.id)}: ${value(check.outcome)}${check.reason ? ` (${check.reason})` : ""}`)));
    card.appendChild(caseChips(item.citations)); propBody.appendChild(card); });
  grid.appendChild(panel("Proposals", "APPROVAL STATE · NO ACTIONS HERE", propBody));
  const findingBody = el("div", "stack"), findingGroups = caseFindingGroups(view);
  if (!findingGroups.length) findingBody.appendChild(empty("No findings yet", "The investigation has not recorded a finding."));
  findingGroups.forEach(group => { const section = el("section", "case-group"); section.dataset.status = group.status;
    append(section, append(el("h3", ""), tag(group.label), el("span", "text-muted", ` ${group.items.length} · ${group.hint}`)));
    group.items.forEach(item => { const card = el("article", "case-finding");
      append(card, append(el("div", "case-tags"), el("small", "text-muted", `${value(item.id)} · ${item.label === "model_output_interpretation" ? "interpretation (model output)" : "cited fact"}`)),
        el("p", "", item.statement === null || item.statement === undefined ? "Content withheld: a citation does not resolve for you now." : item.statement),
        caseChips(item.citations));
      section.appendChild(card); });
    findingBody.appendChild(section); });
  grid.appendChild(panel("Findings", `${findingGroups.reduce((sum, group) => sum + group.items.length, 0)} BY STATUS`, findingBody));
  const calcs = array(view.calculations);
  if (calcs.length) { const calcBody = el("div", "stack");
    calcs.forEach(calc => { const card = el("article", "case-finding");
      append(card, el("small", "text-muted", value(calc.id)),
        el("p", "", calc.expression ? `${calc.expression} = ${value(calc.result)} ${value(calc.unit)}` : "Calculation withheld: a cited input does not resolve for you now."),
        caseChips(Object.values(calc.inputs || {}).filter(spec => spec && spec.kind === "citation").map(spec => spec.citation)));
      calcBody.appendChild(card); });
    grid.appendChild(panel("Calculations", "DECIMAL · FROM CITED ROWS", calcBody)); }
  const coverage = caseCoveragePanel(view.source_coverage);
  if (coverage) grid.appendChild(coverage);
  const summary = caseEvaluationSummary(view.evaluation), evalBody = el("div", "stack");
  append(evalBody, append(el("div", "case-tags"), el("span", `tag ${summary.tone}`.trim(), summary.headline)), ...summary.lines.map(line => el("p", "", line)));
  grid.appendChild(panel("Evaluation", "LATEST STORED PLAYBOOK REPORT", evalBody));
  root.appendChild(grid);
  const limits = el("details", "case-limits"); const schemes = array(view.limitations).filter(line => /:\/\//.test(line));
  // Citation-scheme notes (api://, graph-mail://) are for an engineer; the rest is plain.
  append(limits, el("summary", "", "Limitations of this view"), ...array(view.limitations).filter(line => !/:\/\//.test(line)).map(line => el("p", "text-muted", line)),
    schemes.length ? append(el("details", "engineer"), el("summary", "", "Engineer detail: citation sources"), ...schemes.map(line => el("p", "text-muted", line))) : null);
  root.appendChild(limits);
  return root;
}
function casesView(target) {
  const data = state.cases || {available: false, cases: []};
  if (!data.available) { target.appendChild(empty("Case workspace not configured", "Start the console with python3 -m operator_ui.case_console to read durable cases.")); return; }
  if (state.caseView) {
    const back = el("button", "button subtle", "← All cases"); back.type = "button";
    back.addEventListener("click", () => { state.caseView = null; setRoute(true); render(); });
    append(target, back, renderCaseWorkspace(state.caseView)); return;
  }
  target.appendChild(caseEvidenceElement(data));
  target.appendChild(panel("Cases", `${array(data.cases).length} VISIBLE TO YOU`, list(array(data.cases), item =>
    row({title: caseTitle(item)}, item.status === "closed" ? "✓" : "◎", `${caseQuestions(item.question).length} question(s) · ${item.events} events · updated ${etTime(item.updated_at)} · ${item.case_id}`, "Open →", () => openCase(item.case_id, true)),
    "No cases visible", "Cases appear when an investigation records one for your principals.")));
}
// Home: /api/home answers the signed-in session's open cases (with their open gaps) and the
// gaps the workbench detected by rule (operating/proactive_gaps.py), each cited.
const GAP_KIND = {missing_in_record: "Missing in the record", conflicting_quantity: "Conflicting quantities", expired_quote: "Expired quote",
  unconfirmed_po: "Unconfirmed purchase order", late_po: "Late purchase order", po_receipt_quantity_mismatch: "Receipt quantity differs from the PO",
  receipt_without_qc: "Receipt with no quality check", order_without_customer_po: "Order with no customer PO",
  systems_disagree: "Systems disagree"};
// The scheduled rules, named for a reader, and why one did not run.
const HOME_RULE = {late_or_unconfirmed_po: "late or unconfirmed purchase orders", po_receipt_quantity_mismatch: "PO against receipt quantities",
  receipt_without_qc: "receipts with no quality check", order_without_customer_po: "orders with no customer PO", expired_quote: "expired quotes",
  conflicting_quantity: "conflicting quantities", card_rules: "values missing on documents"};
const HOME_RULE_STATE = {not_configured: "not run on this schedule", blocked: "a source it needs is not served", not_run: "has not run yet"};
function homeFoundRows(proactive) {
  return array(proactive && proactive.gaps).map(gap => ({id: gap.id, kind: GAP_KIND[gap.kind] || gap.kind, summary: gap.summary,
    cites: array(gap.citations).map(handle => ({handle, status: "resolved"})), followUp: gap.follow_up}));
}
// A scan that stopped at its bound, failed reads or left rules unrun says so: never a clean "No gaps found".
function homeScanIncomplete(proactive) {
  const unread = Object.entries((proactive && proactive.unread) || {});
  const notRun = Object.entries((proactive && proactive.not_run) || {});
  const rule = name => HOME_RULE[name] || name;
  const parts = [];
  if (proactive && proactive.truncated && proactive.max_objects == null) {
    const cut = Object.entries(proactive.detectors || {}).filter(([, status]) => status === "truncated").map(([name]) => rule(name));
    if (cut.length) parts.push(`${cut.length} rule${cut.length === 1 ? "" : "s"} stopped short of every record (${cut.join(", ")})`);
  } else if (proactive && proactive.truncated) parts.push(`the scan stopped at its bound of ${value(proactive.max_objects)} records with ${value(proactive.unscanned)} found records unread`);
  if (unread.length) parts.push(`${unread.reduce((n, [, count]) => n + count, 0)} reads failed (${unread.map(([code, count]) => `${code}: ${count}`).join(", ")})`);
  if (notRun.length) parts.push(`${notRun.length} rule${notRun.length === 1 ? "" : "s"} did not run, so found nothing: ${notRun.map(([name, status]) => `${rule(name)} (${HOME_RULE_STATE[status] || status})`).join(", ")}`);
  if (!parts.length && proactive && proactive.complete === false) parts.push("not every rule ran to the end");
  return parts.length ? `Incomplete: ${parts.join("; ")}.` : "";
}
// What the scan read, said first: a scan of 0 records found nothing because it read
// nothing, never "No gaps found".
function homeScanned(proactive) {
  if (!proactive || proactive.status !== "read") return "";
  const scanned = Number(proactive.scanned) || 0;
  const late = (proactive.detectors || {}).late_or_unconfirmed_po;
  const lateWords = !late ? "" : late === "complete" ? ` The late-order rule ran over all ${scanned}.`
    : late === "truncated" ? " The late-order rule stopped short of every record."
    : ` The late-order rule did not run (${HOME_RULE_STATE[late] || String(late).replace(/_/g, " ")}).`;
  return `Scanned ${scanned} record${scanned === 1 ? "" : "s"} you may read${proactive.as_of ? ` as of ${etTime(proactive.as_of)}` : ""}.${lateWords}`;
}
// What a Propose did, in words: proposed and waiting for approval, or recorded but unable to
// request approval yet, and why (proposal.not_prepared).
function proposedWords(data) {
  const p = (data && data.proposal) || {};
  if (p.not_prepared) {
    const W = globalThis.PLXWorkspace;
    const why = W && W.notPreparedWords ? W.notPreparedWords(p.not_prepared) : String(p.not_prepared).replace(/_/g, " ");
    return `Recorded${p.case_id ? ` on case ${p.case_id}` : ""}, but it cannot request approval yet: ${why}. Nothing was executed.`;
  }
  return `Follow-up proposed${p.case_id ? ` on case ${p.case_id}` : ""}: it waits for a person's approval in Mission Control. Nothing was executed.`;
}
function homeWithheldSources(proactive) {
  const sources = array(proactive && proactive.withheld_sources);
  return sources.length ? `Not granted to you on this console: ${sources.map(source => source.label || source.source).join(", ")}.` : "";
}
function homeFoundList(proactive) {
  const status = proactive && proactive.status;
  const note = {not_configured: "The object workspace is not served here.", refused: "You may not scan the object workspace.",
    unreadable: `The scan could not be read now (${value(proactive && proactive.error)}).`}[status];
  if (note) return empty("No scan", note);
  if (status !== "read") return empty("No scan", "The scan answered nothing this console reads.");
  const incomplete = homeScanIncomplete(proactive);
  const rows = homeFoundRows(proactive);
  if (!rows.length && incomplete) return empty("Scan incomplete", `No gaps in the ${value(proactive.scanned)} records read. ${incomplete}`);
  if (!rows.length && !(Number(proactive.scanned) > 0)) return empty("No gaps could be looked for",
    `The workbench read 0 records you may read, so it could not look for gaps: this is not a clean result. ${homeWithheldSources(proactive)}`.trim());
  const found = list(rows, item => {
    const box = el("div", "case-gap"); box.dataset.gap = item.id;
    const said = el("small", "home-propose-status"), propose = proposeButton(item.followUp, said);
    if (!item.followUp) { propose.disabled = true; said.textContent = "Nothing to propose: this gap carries no follow-up."; }
    return append(box, tag(item.kind), el("strong", "", item.summary), caseChips(item.cites), propose, said);
  }, "No gaps found", `Rules found nothing missing, conflicting or expired in the ${value(proactive.scanned)} records scanned.`);
  return incomplete ? append(el("div", "stack"), empty("Scan incomplete", incomplete), found) : found;
}
function homeCaseList(data) {
  if (!data || data.status !== "read") return empty("No case list", "This console does not serve cases to you, or could not read them now.");
  return list(array(data.cases), item => {
    const box = el("div", "stack"), gaps = array(item.gaps);
    box.appendChild(row({title: caseTitle(item)}, "◎", `${gaps.length} open gaps · updated ${etTime(item.updated_at)} · ${item.case_id}`, "Open →", () => { changeView("cases", true); openCase(item.case_id); }));
    if (gaps.length) { const raw = el("details", "home-case-gaps");
      raw.appendChild(el("summary", "", `Open gaps on this case · ${gaps.length}`));
      gaps.forEach(gap => { const {card, words} = gapCard(gap, "case-gap", [tag(CASE_GAP_STATES[gap.state] || gap.state)]);
        card.appendChild(caseChips(gap.citations));
        if (words.engineer) card.appendChild(engineerDetail(gap));
        raw.appendChild(card); });
      box.appendChild(raw); }
    return box;
  }, "No open cases", "Cases appear when an investigation records one for your principals.");
}
// Home's objective box (POST /api/objective): a free-form objective, no object id; the
// agent finds the records itself and answers with cited claims, gaps and follow-ups it
// only proposes. A scenario lists each quoted input with its record's date and state.
const OBJECTIVE_INPUT_STATES = {current: "current", stale: "stale", unknown: "date unknown", expired: "expired", expires_before_target: "expires before target"};
function objectiveCites(citations) { return caseChips(array(citations).map((cite, index) => cite.openable ? {handle: cite.l1_handle, status: "resolved"} : {index: index + 1})); }
function objectiveFollowUps(items) {
  return list(array(items), item => followUpCard(item.proposal || {}),
    "No follow-ups proposed", "Nothing the agent left open names a record and who could answer it.");
}
// A record the objective reached, by its kind and the number it was named by, with an
// Open link; its object id only in an engineer's detail.
function objectiveRoot(root) {
  const box = el("div", "case-gap"), open = el("button", "button", "Open →");
  open.type = "button"; open.addEventListener("click", () => changeView("objects", true, {object: root.object_id}));
  const named = root.mention ? `${typeWords(root.object_type)} ${root.mention}` : `A ${typeWords(root.object_type).toLowerCase()} the agent cited`;
  const raw = el("details", "engineer"); append(raw, el("summary", "", "Engineer detail"), el("code", "", `${value(root.object_type)} · ${root.object_id}`));
  return append(box, tag(root.found_by === "agent" ? "found by the agent" : "named"), el("strong", "", named), open, raw);
}
function objectiveResult(data) {
  const box = el("div", "stack"), answer = data.answer || {}, scenario = data.scenario;
  box.dataset.objective = "result";
  partAnswers(data).forEach(card => box.appendChild(card));
  append(box, tag(answer.outcome), el("small", "text-muted", `${array(data.roots).length} records found · as of ${value(data.as_of)}${data.target && data.target.date ? ` · target ${data.target.date}` : ""}`));
  box.appendChild(list(array(data.roots), objectiveRoot,
    "No record found", "No identifier in the objective named a record you may read, and the agent cited none."));
  box.appendChild(list(array(answer.claims), claim => append(el("div", "case-gap"), el("strong", "", plainText(claim.text)), objectiveCites(claim.citations), rawDetail([[claim.text, plainText(claim.text)]])),
    "No cited answer", `The agent ${value(answer.outcome)} (${value(answer.reason)}): nothing below is answered by a record.`));
  if (scenario) {
    box.appendChild(el("h3", "", "Inputs the records quote"));
    box.appendChild(list(array(scenario.inputs), item => append(el("div", "case-gap"), tag(OBJECTIVE_INPUT_STATES[item.state] || item.state),
      el("strong", "", `${plainText(item.field)} = ${value(item.value)}${item.unit ? " " + item.unit : ""}`),
      rawDetail([[`${value(item.field)} = ${value(item.value)}`, `${plainText(item.field)} = ${value(item.value)}`]]),
      el("small", "text-muted", item.source_date ? `source date ${item.source_date} from ${value(item.source_date_field)} (${item.age_days} days old)` : array(item.conflicting_dates).length ? `dates disagree: ${array(item.conflicting_dates).map(d => `${d.field} ${d.date}`).join(", ")}` : "no quote or document date on its record"),
      caseChips(item.openable ? [{handle: item.citation, status: "resolved"}] : [{index: 1}])),
    "No quoted inputs", "No claim the agent cited quoted a value a plan could reuse."));
    box.appendChild(el("h3", "", "A person must confirm"));
    box.appendChild(list(array(scenario.must_confirm), item => append(el("div", "case-gap"), tag(item.state), el("strong", "", plainText(item.what)), el("small", "text-muted", plainText(item.why)), rawDetail([[item.what, plainText(item.what)]])),
      "Nothing to confirm", "Every input is current and dated."));
  }
  box.appendChild(el("h3", "", "Gaps"));
  box.appendChild(list(array(data.gaps), gap => append(el("div", "case-gap"), tag(gap.kind), el("strong", "", plainText(gap.subject)), el("small", "text-muted", plainText(gap.reason))),
    "No gaps", "The agent left nothing of the objective unanswered."));
  box.appendChild(el("h3", "", "Proposed follow-ups"));
  box.appendChild(objectiveFollowUps(data.follow_ups));
  return box;
}
// The question box has one path: every question posts to /api/objective. The server
// answers each part it covers (a plan's verdict, a record page's answer) for each record
// the question names by its number, and the agent runs beside it; the browser routes
// nothing. A part the server left to the agent says so (parts.left_to_agent).
const PART_WORDS = {late: "lateness", landed_cost: "landed cost", margin: "margin", missing: "what is missing", supplier: "the supplier"};
function partWords(part) { return PART_WORDS[part] || String(part || "").replace(/_/g, " "); }
// The server's answer cards, each verdict line first, before the agent's result.
function partAnswers(data) {
  const cards = [];
  array(data.answers).forEach(entry => array(entry && entry.parts).forEach(item => {
    const answer = item && item.answer && typeof item.answer === "object" ? item.answer
      : {state: "not_established", text: "Not established: the server returned no answer for this part"};
    cards.push(answerCard(answer, item && item.why, [`part ${value(item && item.part)}`, item && item.family ? `family ${item.family}` : "record page", `root ${value(entry.object_id)}`]));
  }));
  const parts = data.parts || {}, left = array(parts.left_to_agent);
  if (left.length) {
    const note = el("p", "text-muted", `Left to the agent: ${left.map(partWords).join(", ")}${array(data.answers).length ? " (no server answer covers it for the records named)" : " (the question names no single record by its number)"}.`);
    note.dataset.objective = "left";
    cards.push(note);
  }
  return cards;
}
// Server prose with a field path ("row.unit_price") or an object id in plain words; the
// raw text stays in an engineer's detail where it changed.
function plainText(text) {
  const W = globalThis.PLXWorkspace, raw = text === null || text === undefined ? "" : String(text);
  const ids = W && W.humanize ? W.humanize(raw) : raw;
  return ids.replace(/\brow\.([A-Za-z][A-Za-z0-9_.]*)/g, (_, path) => path.split(".").pop()
    .replace(/([a-z])([A-Z])/g, "$1 $2").replace(/_/g, " ").toLowerCase());
}
function rawDetail(pairs) {
  const shown = pairs.filter(([raw, said]) => raw && String(raw) !== said);
  if (!shown.length) return null;
  const box = el("details", "engineer");
  box.appendChild(el("summary", "", "Engineer detail"));
  shown.forEach(([raw]) => box.appendChild(el("code", "", String(raw))));
  return box;
}
// One Propose per follow-up: the click disables it at once (a second click posts nothing)
// and an answer drawn again shows it Proposed without posting again (state.proposed). A
// refusal says why in one plain sentence, never a silent failure.
const FOLLOW_UP_REFUSED = [
  [/follow_up_draft_not_derived/, "No supplier draft could be built: the records do not show which order lines are late, so nothing was proposed."],
  [/follow_up_draft_citation_unresolved/, "No supplier draft could be built: a record it would cite is not one you may open, so nothing was proposed."],
  [/follow_up_draft_invalid/, "No supplier draft could be built from this answer, so nothing was proposed."],
  [/follow_up_refused:([a-z_]+)/, code => `The follow-up was refused (${code.replace(/_/g, " ")}), so nothing was proposed.`]];
function followUpRefusal(error, proposal) {
  const text = String(error && error.message || error || "");
  for (const [pattern, said] of FOLLOW_UP_REFUSED) { const found = pattern.exec(text); if (found) return typeof said === "function" ? said(found[1]) : said; }
  if (proposal && proposal.draft && /^service_error$|follow_up_draft_not_served/.test(text)) return "No supplier draft could be built: this console does not word supplier drafts, so nothing was proposed.";
  return "";
}
function proposeButton(proposal, said, mark) {
  const key = JSON.stringify(proposal || null), button = el("button", "button", "Propose follow-up");
  button.type = "button";
  const shown = words => { button.disabled = true; button.textContent = "Proposed"; said.textContent = words; if (mark) { mark.textContent = "Proposed"; mark.className = "tag amber"; } };
  if (state.proposed[key]) shown(state.proposed[key]);
  button.addEventListener("click", async () => {
    if (!proposal || button.disabled || state.proposed[key]) return;
    button.disabled = true; said.textContent = "Proposing…";
    try {
      const data = await post("/api/object-follow-up", proposal), caseId = data && data.proposal && data.proposal.case_id;
      state.proposed[key] = proposedWords(data); shown(state.proposed[key]);
      notice(`Follow-up proposed${caseId ? ` on case ${caseId}` : ""}. Nothing was sent or executed.`);
    } catch (error) {
      const refused = followUpRefusal(error, proposal), W = globalThis.PLXWorkspace;
      said.textContent = refused || `Not proposed: ${W && W.plainError ? W.plainError(error) : error.message}`;
      if (!refused) button.disabled = false;
    }
  });
  return button;
}
// A follow-up the answer proposes, shown as the message it would be: to whom, about what
// and why. A supplier draft is worded by the server from the records when proposed.
function followUpCard(proposal) {
  const box = el("div", "case-gap ow-follow-up"), said = el("small", "home-propose-status"), mark = tag("Draft · never sent");
  box.dataset.followUp = "proposal";
  const lines = el("dl", "ow-draft"), subject = plainText(proposal.missing), body = plainText(proposal.why);
  [["To", `${array(proposal.who_can_answer).join(", ") || "—"} (who can answer)`], ["Subject", subject], ["Body", body]]
    .forEach(([name, text]) => append(lines, el("dt", "", name), el("dd", "", text)));
  if (proposal.draft) append(lines, el("dt", "", "Supplier draft"), el("dd", "", `${value(proposal.draft.label)}: worded from the cited records when you propose`));
  return append(box, mark, lines, rawDetail([[proposal.missing, subject], [proposal.why, body]]), proposeButton(proposal, said, mark), said);
}
function factChips(fact) { return caseChips([fact.citation && fact.openable ? {handle: fact.citation, status: "resolved"} : {index: 1}]); }
// The answer card: the verdict line first, then what it is about (by its number, with an
// Open link), the cited facts, "Why this answer" collapsed and the follow-ups.
function answerCard(answer, why, engineer) {
  const box = el("div", "stack ow-answer"), subject = answer.subject || {};
  box.dataset.objective = "answer";
  const verdict = el("p", `ow-verdict ${answer.state || ""}`.trim(), value(answer.text));
  verdict.dataset.verdict = answer.state || "unknown";
  const head = el("div", "ow-answer-head");
  head.appendChild(el("strong", "", value(subject.label)));
  if (subject.object_id) { const open = el("button", "button", "Open →"); open.type = "button";
    open.addEventListener("click", () => changeView("objects", true, {object: subject.object_id})); head.appendChild(open); }
  append(box, verdict, head);
  box.appendChild(list(array(answer.facts), fact => append(el("div", "case-gap"), el("span", "", plainText(fact.text)), fact.citation ? factChips(fact) : null),
    "No cited values", "No record you may read answers this question."));
  const details = el("details", "ow-why");
  details.appendChild(el("summary", "", "Why this answer"));
  array(why).forEach(item => details.appendChild(el("p", "text-muted", plainText(item && item.text))));
  if (engineer) { const raw = el("details", "engineer"); raw.appendChild(el("summary", "", "Engineer detail"));
    engineer.forEach(line => raw.appendChild(el("code", "", line))); details.appendChild(raw); }
  box.appendChild(details);
  const followUps = array(answer.follow_ups);
  if (followUps.length) { box.appendChild(el("h3", "", "Proposed follow-ups")); followUps.forEach(item => box.appendChild(followUpCard(item))); }
  return box;
}
function sentence(text, cls) { const box = el("div", "stack ow-answer"); box.dataset.objective = "answer"; box.appendChild(el("p", `ow-verdict ${cls || "not_established"}`, text)); return box; }
// Shown in the box's answer slot and scrolled into view, so the answer is where the eye is.
async function showAnswer(slot, build, settled) {
  if (!slot) return;
  clear(slot); slot.appendChild(el("small", "text-muted", "Answering…"));
  let shown;
  try { shown = await build(); } catch (error) { const W = globalThis.PLXWorkspace;
    shown = sentence(`Not answered: ${W && W.plainError ? W.plainError(error) : error.message}`); }
  clear(slot); slot.appendChild(shown);
  // Settle the layout above (the status line) first, so the scroll lands on the answer.
  if (settled) settled();
  if (slot.scrollIntoView) slot.scrollIntoView({behavior: "smooth", block: "start"});
  return shown;
}
function homeObjective(info) {
  if (!info || !info.served) return empty("Objective box not served", {not_configured: "The object workspace is not served here.", no_agent_planner: "This console runs no agent: start it with --ask."}[info && info.reason] || "This console does not run objectives.");
  const form = el("form", "ow-ask"), objective = el("textarea"), mode = el("select"), deadline = el("input");
  objective.setAttribute("rows", "2"); objective.setAttribute("aria-label", "Objective");
  objective.setAttribute("placeholder", "Ask a question or give an objective, e.g. why is a purchase order (by its number) late and what is missing, or air-ship the next order by Nov 15");
  array(info.modes).forEach(name => { const option = el("option", "", name); option.value = name; mode.appendChild(option); });
  mode.value = "investigate"; mode.setAttribute("aria-label", "Mode");
  deadline.type = "date"; deadline.setAttribute("aria-label", "Target date (optional)");
  const run = el("button", "button", "Run objective"); run.type = "submit";
  const said = el("small", "ow-review-status"), slot = el("div", "ow-answer-slot");
  form.addEventListener("submit", async event => {
    if (event && event.preventDefault) event.preventDefault();
    const text = String(objective.value || "").trim();
    if (!text) { said.textContent = "Write an objective first."; return; }
    const body = {objective: text, mode: mode.value || "investigate"};
    if (deadline.value) body.deadline = deadline.value;
    said.textContent = "Investigating…"; run.disabled = true; clear(slot);
    try { await showAnswer(slot, () => post("/api/objective", body).then(objectiveResult), () => { said.textContent = ""; }); }
    catch (error) { said.textContent = error.message; }
    finally { run.disabled = false; }
  });
  return append(form, objective, mode, deadline, run, said, slot);
}
function homeView(target) {
  if (!state.home) return target.appendChild(empty("Loading", "Reading your cases and gaps."));
  const data = state.home, proactive = data.proactive;
  const found = el("div", "stack"), scanned = homeScanned(proactive);
  if (scanned) found.appendChild(el("p", "home-scanned", scanned));
  found.appendChild(homeFoundList(proactive));
  const foundPanel = panel("Found by the workbench", "RULES OVER RECORDS YOU MAY READ", found);
  foundPanel.dataset.home = "proactive";
  const casesPanel = panel("Open cases", `${value(data.cases && data.cases.open_cases)} OPEN · ${value(data.cases && data.cases.open_gaps)} OPEN GAPS`, homeCaseList(data.cases));
  casesPanel.dataset.home = "cases";
  const objectivePanel = panel("Objective", "THE AGENT FINDS THE RECORDS · READ-ONLY", homeObjective(data.objective));
  objectivePanel.dataset.home = "objective";
  append(target, objectivePanel, foundPanel, casesPanel);
}
async function loadHome() {
  try { state.home = await api("/api/home"); if (state.view === "home") render(); }
  catch (error) { notice(error.message); }
}
async function loadCases() {
  try { state.cases = await api("/api/cases"); const count = $("case-count"); if (count) count.textContent = array(state.cases.cases).length; if (state.view === "cases") render(); }
  catch (error) { notice(error.message); }
}
async function refreshCases() {
  await loadCases();
  if (state.caseView) await openCase(state.caseView.case_id);
}
async function openCase(caseId, push) {
  try { const view = await api(`/api/case/${encodeURIComponent(caseId)}`);
    view.status_cards = await loadCaseStatusCards(view);
    state.caseView = view; setRoute(push); if (state.view === "cases") render(); }
  catch (error) { state.caseView = null; setRoute(); if (state.view === "cases") render(); notice(error.message); }
}
// Back/Forward or an edited address: show that route again (no new history entry). Back
// fires both popstate and hashchange: the calls run one after another, and the second
// finds the route already shown.
let routeQueue = Promise.resolve();
function applyRoute() { routeQueue = routeQueue.then(showRoute, showRoute); return routeQueue; }
async function showRoute() {
  if (!state.sessionVerified) return;
  const next = parseRoute(location.hash);
  if (next.view !== state.view) {
    if (next.view !== "cases") state.caseView = null;
    changeView(next.view, false, next.objectRoute);
  } else if (next.view === "objects") {
    state.objectRoute = next.objectRoute;
    if (state.objects && state.objects.applyRoute) await state.objects.applyRoute(next.objectRoute);
    setRoute();
  }
  if (next.view === "cases") {
    if (next.caseId && state.caseWorkspace) { if (!state.caseView || state.caseView.case_id !== next.caseId) await openCase(next.caseId); }
    else if (state.caseView) { state.caseView = null; render(); }
  }
}
async function openCaseCitation(handle) {
  try {
    const data = await api(`/api/case-citation?handle=${encodeURIComponent(handle)}`);
    const body = el("div", "detail-content"); body.dataset.detailKey = `citation:${handle}`;
    append(body, tag(data.kind === "original" ? (data.text_source === "ocr" ? "original · OCR text" : "original") : "source row"), el("h2", "", citationLabel(handle)), el("small", "text-muted case-handle", handle));
    if (data.kind === "row") {
      if (data.status) body.appendChild(detailSection("How current this copy is", {"state": data.status.status, "changed since cited": data.status.changed_since,
        "last refreshed": data.status.relation_last_refresh_at ? etTime(data.status.relation_last_refresh_at) : data.status.relation_last_refresh_at,
        "deletions proven": data.status.deletions_proven}));
      array(data.row ? [data.row] : data.rows).forEach(item => body.appendChild(detailSection(`${data.relation}`, item)));
    } else {
      if (data.note) body.appendChild(el("p", "callout", data.note));
      body.appendChild(detailSection("Original", {filename: data.filename, sha256: data.sha256, bytes: data.bytes, page: data.page, text_source: data.text_source}));
      if (typeof data.text === "string") { const pre = el("pre", "case-page-text", data.text || "(no text on this page)"); pre.setAttribute("tabindex", "0"); body.appendChild(pre); }
      if (array(data.page_citations).length) { const pages = el("div", "case-chips");
        data.page_citations.forEach(page => pages.appendChild(caseCitationChip({handle: page, status: "resolved"}))); body.appendChild(pages); }
    }
    openDetail(body);
  } catch (error) { notice(error.message); }
}

document.addEventListener("DOMContentLoaded", async () => {
  const start = parseRoute(window.location.hash);
  const view = start.view, caseId = start.caseId;
  // A link printed by the console carries the bearer once: keep it for this tab, drop it
  // from the address bar (and so from history and bookmarks).
  if (start.access !== null) keepAccess(start.access);
  else state.accessToken = storedAccess();
  state.view = view; state.objectRoute = start.objectRoute; setRoute();
  window.addEventListener("popstate", () => { applyRoute(); });
  window.addEventListener("hashchange", () => { applyRoute(); });
  document.querySelectorAll(".nav-item").forEach(button => button.addEventListener("click", () => changeView(button.dataset.view, true)));
  $("refresh").addEventListener("click", refresh);
  $("back-detail").addEventListener("click", backDetail);
  $("close-detail").addEventListener("click", closeDetail);
  const channel = accessChannel();
  answerAccess(channel);
  if (!state.accessToken) { const shared = await askAccess(channel); if (shared) keepAccess(shared); }
  try {
    const session = await api("/api/session");
    state.token = session.csrf_token;
    state.sessionVerified = true;
    $("session-status").textContent = sessionLabel(session.identity);
    $("session-role").textContent = sessionRole(session.principals);
    state.caseWorkspace = Boolean(session.case_workspace);
    changeView(view);
    if (state.caseWorkspace) await loadCases();
    if (view === "cases" && caseId && state.caseWorkspace) await openCase(caseId);
  } catch (e) { notice(e.message); }
});
