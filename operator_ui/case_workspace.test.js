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
    this.attributes = {};
    this.classList = {contains: () => false, toggle: () => {}, remove: () => {}, add: () => {}};
  }
  appendChild(child) { this.children.push(child); return child; }
  removeChild(child) { this.children.splice(this.children.indexOf(child), 1); return child; }
  addEventListener(event, handler) { this.listeners[event] = handler; }
  setAttribute(name, value) { this.attributes[name] = value; }
  get firstChild() { return this.children[0] || null; }
  get firstElementChild() { return this.firstChild; }
  get childNodes() { return this.children; }
}

function walk(node) { return [node, ...node.children.flatMap(walk)]; }
function content(node) { return walk(node).map(part => part.textContent).join(" "); }
// Values built inside the vm context have that realm's prototypes; compare plain copies.
function plain(value) { return JSON.parse(JSON.stringify(value)); }

function load(fetchImpl) {
  const document = {createElement: tag => new Element(tag),
    getElementById: () => new Element("div"), addEventListener: () => {}};
  const context = vm.createContext({document, URL, URLSearchParams, fetch: fetchImpl,
    setTimeout});
  vm.runInContext(fs.readFileSync(path.join(__dirname, "app.js"), "utf8"), context);
  return context;
}

const AO = "api://public.assembly_order/%7B%22id%22%3A10718%7D@g1-1";
const ORDER = "api://public.order_entry/%7B%22id%22%3A351%7D@g1-2";
const ITEM = "api://silver.assembly_order_items/%7B%22id%22%3A58383%7D@g1-5";
const PAGE = "original://abc123def4567890#page=1";
const FILE = "original://abc123def4567890";

function caseView() {
  const resolved = handle => ({handle, status: "resolved", digest: "d"});
  return {
    case_id: "case-1", question: "Was AO-26005B produced as ordered?", roots: ["AO-26005B", "OR1000151885"],
    status: "open", opened_at: "2026-10-01T10:00:00+00:00", updated_at: "2026-10-01T10:05:00+00:00",
    outcome: null, integrity: {events: 6, chain_ok: true},
    findings: {
      supported: [{id: "F1", kind: "fact", label: "cited_fact", status: "supported",
        statement: "AO-26005B is linked to sales order 351.", citations: [resolved(AO), resolved(ORDER)]},
      {id: "F4", kind: "fact", label: "cited_fact", status: "supported",
        statement: "The scan reads PLANNED 1509.", citations: [resolved(PAGE), resolved(ITEM)]}],
      contradicted: [{id: "F2", kind: "fact", label: "cited_fact", status: "contradicted",
        statement: "Produced on time.", citations: [resolved(ORDER)]}],
      unknown: [], superseded: [], citation_content_changed: [],
      withheld: [{id: "F3", kind: "fact", label: "cited_fact", status: null, statement: null,
        content: "withheld_citation_unavailable", citations: [{index: 0, status: "citation_unavailable"}]}]},
    calculations: [{id: "C1", expression: "planned - manufactured", result: "6", unit: "EA",
      inputs_status: "resolved", inputs: {planned: {kind: "citation", citation: resolved(ITEM)}}}],
    gaps: [{id: "G1", state: "unknown", missing: "Why production was late", why: "No reason recorded.",
      who_can_answer: ["Production planning"], citations: [resolved(AO), resolved(ORDER)], citations_status: "resolved"},
    {id: "G2", state: "absent_in_source", missing: "AO header quantities", why: "Both empty.",
      who_can_answer: ["Ricardo (Quality)"], citations: [resolved(AO)]},
    {id: "G3", content: "withheld_citation_unavailable", missing: null, why: null, who_can_answer: null,
      state: null, citations: [{index: 0, status: "citation_unavailable"}]}],
    proposals: [{id: "P1", status: "proposed", capability: "portal.assembly_order.set_header_quantities",
      payload: {qtytotal: "1509"}, preconditions: ["Quality confirms"], citations: [resolved(AO), resolved(ITEM)],
      approval: {state: "needs_human_approval", where: "Mission Control",
        note: "Recorded only. Needs human approval in Mission Control; the Workbench never approves, dispatches or executes it."},
      verifications: []}],
    verifications: [],
    timeline: [{seq: 1, at: "2026-10-01T10:00:00+00:00", actor: "staff:operations", kind: "case.opened", ref: null},
      {seq: 2, at: "2026-10-01T10:01:00+00:00", actor: "staff:operations", kind: "finding.recorded", ref: "F1"}],
    records: [{handle: AO, relation: "public.assembly_order", key: {id: 10718}, cited_by: []},
      {handle: ORDER, relation: "public.order_entry", key: {id: 351}, cited_by: []},
      {handle: ITEM, relation: "silver.assembly_order_items", key: {id: 58383}, cited_by: []}],
    source_dates: [{at: "2026-01-05", column: "datemanuf", relation: "public.assembly_order", key: {id: 10718}, handle: AO},
      {at: "2026-01-01", column: "datereq", relation: "public.order_entry", key: {id: 351}, handle: ORDER}],
    originals: [{citation: FILE, filename: "AO-26005B scan.pdf", sha256: "f".repeat(64), bytes: 1000,
      extraction: {status: "extracted", method: "ocr", ocr_pages: [1]},
      pages: [{citation: PAGE, page: 1, text_source: "ocr"}], cited_as: [PAGE]}],
    evaluation: {status: "stored", passed_all: true, passed_all_clean: false, passed_all_qualification: "authority_not_checked",
      limits: [], checks: 15, passed: 15, failed: 0, failed_ids: [], playbook_id: "ao_quantity_reconciliation",
      playbook_version: "1", evaluator_version: "e/6", recorded_at: "2026-10-01T11:00:00Z", recorded_by: "staff:quality",
      transcript_source: "none", freshness: "current", evaluated_events: 2, current_events: 2},
    limitations: ["Advisory: mirror pages are non-atomic."]};
}

test("citation labels name the relation and key, or the original page", () => {
  const app = load();
  assert.equal(app.citationLabel(AO), "public.assembly_order · id=10718");
  assert.equal(app.citationLabel(PAGE), "original abc123def456 · page 1");
  assert.equal(app.citationLabel("original://abc123def4567890#extraction=2&page=3"), "original abc123def456 v2 · page 3");
});

test("timeline merges case events with cited source dates in chronological order", () => {
  const app = load();
  const entries = app.caseTimeline(caseView());
  assert.deepEqual(plain(entries.map(entry => [entry.at, entry.source])), [
    ["2026-01-01", "source"], ["2026-01-05", "source"],
    ["2026-10-01T10:00:00+00:00", "case"], ["2026-10-01T10:01:00+00:00", "case"]]);
  assert.equal(entries[0].label, "public.order_entry · datereq");
  assert.equal(entries[0].handle, ORDER);
  assert.equal(entries[3].label, "finding recorded · F1");
});

test("findings are grouped with contradicted and withheld before supported; empty groups are omitted", () => {
  const app = load();
  const groups = app.caseFindingGroups(caseView());
  assert.deepEqual(plain(groups.map(group => [group.status, group.items.length])), [["contradicted", 1], ["withheld", 1], ["supported", 2]]);
});

test("gap summary keeps unknown, contradicted and withheld visible", () => {
  const app = load();
  const summary = app.caseGapSummary(caseView());
  assert.equal(summary.gaps.length, 2);
  assert.equal(summary.withheld_gaps, 1);
  assert.equal(summary.unknown, 1);
  assert.equal(summary.contradicted, 1);
  assert.equal(summary.withheld, 2);
  assert.deepEqual(plain(summary.by_state), {unknown: 1, absent_in_source: 1});
  const view = caseView();
  view.calculations.push({id: "C2", content: "withheld_citation_unavailable", result: null, citations: []});
  view.proposals.push({id: "P2", content: "withheld_citation_unavailable", capability: null, citations: []});
  assert.equal(app.caseGapSummary(view).withheld, 4);
});

test("relationships link records co-cited by one item, never through unavailable citations", () => {
  const app = load();
  const view = caseView();
  const rel = app.caseRelationships(view);
  assert.deepEqual(plain(rel.nodes.map(node => node.handle)), [AO, ORDER, ITEM, PAGE]);
  const via = rel.links.map(link => `${link.via.kind}:${link.via.id}`);
  assert.deepEqual(plain(via), ["finding:F1", "finding:F4", "gap:G1", "proposal:P1"]);
  assert.deepEqual(plain(rel.neighbours.get(AO).map(item => item.handle)), [ORDER, ORDER, ITEM]);
  assert.deepEqual(plain(rel.neighbours.get(PAGE).map(item => item.handle)), [ITEM]);
  // A citation that is not resolved for the reader declares no link.
  view.findings.supported[0].citations[1].status = "citation_content_changed";
  assert.deepEqual(plain(app.caseRelationships(view).links.map(link => link.via.id)), ["F4", "G1", "P1"]);
});

test("evaluation summary distinguishes clean, qualified, failed, missing and broken reports", () => {
  const app = load();
  const base = caseView().evaluation;
  const qualified = app.caseEvaluationSummary(base);
  assert.equal(qualified.headline, "Passed all checks, not clean");
  assert.equal(qualified.tone, "amber");
  assert.ok(qualified.lines.includes("Not clean: authority_not_checked"));
  assert.equal(app.caseEvaluationSummary({...base, passed_all_clean: true}).headline, "Passed all checks (clean)");
  const failed = app.caseEvaluationSummary({...base, passed_all: false, failed: 2, passed: 13,
    failed_ids: ["planned_arith", "damage_cited"], limits: ["row_completeness_not_enumerated"]});
  assert.equal(failed.headline, "2 check(s) failed");
  assert.equal(failed.tone, "red");
  assert.ok(failed.lines.includes("Failed: planned_arith, damage_cited"));
  assert.ok(failed.lines.includes("Limits: row_completeness_not_enumerated"));
  assert.equal(app.caseEvaluationSummary({status: "none"}).headline, "Not evaluated yet");
  assert.equal(app.caseEvaluationSummary({status: "integrity_error", detail: "x"}).tone, "red");
  assert.ok(qualified.lines.includes("recorded Oct 1, 2026, 7:00 AM ET by staff:quality · transcript none"));
});

test("an evaluation of an earlier case is labelled stale, never shown as current", () => {
  const app = load();
  const base = {...caseView().evaluation, passed_all_clean: true};
  assert.equal(app.caseEvaluationSummary(base).headline, "Passed all checks (clean)");
  assert.equal(app.caseEvaluationSummary(base).stale, false);
  const stale = app.caseEvaluationSummary({...base, freshness: "stale", evaluated_events: 2,
    current_events: 5, freshness_reason: "evaluated at 2 case events; the case now has 5: it changed after this evaluation"});
  assert.equal(stale.stale, true);
  assert.equal(stale.headline, "Stale evaluation: Passed all checks (clean)");
  assert.equal(stale.tone, "amber");
  assert.match(stale.lines[0], /^Stale: evaluated at 2 case events; the case now has 5/);
  // A report that does not say which events it read is never presented as current.
  const unknown = app.caseEvaluationSummary({...base, freshness: undefined});
  assert.equal(unknown.headline, "Freshness unknown: Passed all checks (clean)");
  assert.equal(unknown.tone, "amber");
  // A failed stale report stays red.
  assert.equal(app.caseEvaluationSummary({...base, passed_all: false, passed_all_clean: false,
    failed: 1, freshness: "stale"}).tone, "red");
});

test("a recorder the workspace does not send is stated as not shown, never a dash", () => {
  const app = load();
  const base = caseView().evaluation;
  for (const recorded_by of [undefined, null, "", "  "]) {
    const lines = app.caseEvaluationSummary({...base, recorded_by}).lines;
    assert.ok(lines.includes("recorded Oct 1, 2026, 7:00 AM ET by a recorder not shown here · transcript none"), lines.join(" | "));
    assert.ok(!lines.some(line => line.includes("by —")));
  }
});

test("workspace renders every part, proposals as records only, and chips resolve through the citation route", async () => {
  const calls = [];
  const app = load(async (url, options) => {
    calls.push([url, options.headers]);
    return {ok: true, status: 200, json: async () => ({kind: "row", relation: "public.assembly_order",
      row: {id: 10718, nameamo_c: "AO-26005B"}, status: {status: "current", changed_since: false}})};
  });
  vm.runInContext("state.accessToken = \"token-1\"", app);
  const root = app.renderCaseWorkspace(caseView());
  const text = content(root);
  for (const part of ["Was AO-26005B produced as ordered?", "AO-26005B", "1 contradicted", "1 unknown",
    "2 withheld", "Gaps", "Why production was late", "Findings", "Contradicted", "Content withheld",
    "Timeline", "datereq", "Relationships", "Originals", "AO-26005B scan.pdf", "Page 1 · OCR",
    "Proposals", "needs human approval in Mission Control", "portal.assembly_order.set_header_quantities",
    "1 gap(s) withheld", "Evaluation", "Passed all checks, not clean", "15 of 15 checks passed", "planned - manufactured = 6 EA"])
    assert.ok(text.includes(part), `missing: ${part}`);
  const buttons = walk(root).filter(node => node.tagName === "button");
  assert.ok(buttons.length > 5);
  assert.equal(buttons.filter(node => /approve|execute|dispatch|send/i.test(node.textContent)).length, 0);
  const chip = buttons.find(node => node.title === AO);
  assert.ok(chip, "a chip for the cited AO row");
  assert.match(chip.attributes["aria-label"], /Open cited source public\.assembly_order/);
  await chip.listeners.click();
  assert.equal(calls[0][0], `/api/case-citation?handle=${encodeURIComponent(AO)}`);
  assert.equal(calls[0][1].Authorization, "Bearer token-1");
  const withheld = walk(root).filter(node => node.className === "case-chip withheld");
  assert.equal(withheld.length, 1); // the withheld finding; withheld gaps are counted, not listed
  assert.equal(withheld[0].listeners.click, undefined);
});

test("markup in case content is rendered as text, never as elements", () => {
  const app = load();
  const payload = '<img src=x onerror="alert(1)"><script>alert(2)</script>';
  const view = caseView();
  view.question = payload;
  view.roots = [payload];
  view.findings.supported[0].statement = payload;
  view.gaps[0].missing = payload;
  view.gaps[0].why = payload;
  view.proposals[0].capability = payload;
  view.proposals[0].payload = {[payload]: payload};
  view.originals[0].filename = payload;
  view.source_dates[0].column = payload;
  view.limitations = [payload];
  const root = app.renderCaseWorkspace(view);
  const nodes = walk(root);
  const tags = new Set(nodes.map(node => node.tagName));
  for (const forbidden of ["img", "script", "iframe", "object"]) assert.ok(!tags.has(forbidden), forbidden);
  const carriers = nodes.filter(node => node.textContent.includes(payload));
  assert.ok(carriers.length >= 8, `payload rendered as text in ${carriers.length} nodes`);
  // Every carrier is a leaf: the payload text never became child elements.
  assert.equal(carriers.filter(node => node.children.length).length, 0);
  assert.equal(app.citationLabel(`original://${payload}#page=1`).includes("<script"), false);
});

test("a withheld evaluation and capped lists are stated, never silently dropped", () => {
  const app = load();
  const withheld = app.caseEvaluationSummary({status: "withheld", reason: "x"});
  assert.equal(withheld.headline, "Evaluation withheld");
  assert.equal(withheld.tone, "amber");
  assert.equal(JSON.stringify(withheld).includes("checks passed"), false);
  const view = caseView();
  view.evaluation = {status: "withheld", reason: "x"};
  view.truncated = {timeline: {shown: 200, total: 950}};
  view.originals[0].pages_truncated = true;
  view.originals[0].pages_total = 120;
  const text = content(app.renderCaseWorkspace(view));
  assert.ok(text.includes("Evaluation withheld"));
  assert.ok(!text.includes("15 of 15 checks passed"));
  assert.ok(text.includes("showing 200 of 950 timeline"));
  assert.ok(text.includes("Showing 1 of 120 pages."));
});

test("an original's PDF opens only through the audience-checked case route", async () => {
  const calls = [];
  const app = load(async (url, options) => {
    calls.push([url, options.headers]);
    return {ok: false, status: 404, json: async () => ({error: "not_found"})};
  });
  vm.runInContext("state.accessToken = \"token-1\"; var window = {open: () => null};", app);
  const root = app.renderCaseWorkspace(caseView());
  const open = walk(root).find(node => node.tagName === "button" && node.textContent === "Open PDF");
  assert.ok(open, "an Open PDF control on the cited original");
  await open.listeners.click();
  assert.equal(calls.length, 1);
  assert.equal(calls[0][0], `/api/case-original-pdf?handle=${encodeURIComponent(FILE)}`);
  assert.equal(calls[0][1].Authorization, "Bearer token-1");
});

// ---- the case view (conflicts, relationships, actions, evidence label) ------------------
const LINK = "api://public.order_entry_item_assembly/%7B%22id%22%3A1245%7D@g1-3";
const DAMAGE = "api://silver.assembly_order_damage/%7B%22id%22%3A5444%7D@g1-6";
const PAGE3 = "original://abc123def4567890#page=3";
function joinedView() {
  const view = caseView();
  view.evidence = {label: "isolated-fixture / injected-transport", fixture: true, note: "FIXTURE: synthetic rows. Not real PLX data."};
  view.gaps.push({id: "G4", state: "unknown", missing: "Which figure for shipper box damage is right: evidence conflict between FM and the paper original",
    why: "The cited FM row(s) record 20; page 3 of the attached original holds the paper figure.", who_can_answer: ["Ricardo (Quality)"],
    citations: [{handle: DAMAGE, status: "resolved"}, {handle: PAGE3, status: "resolved"}]});
  view.conflicts = [{kind: "evidence_conflict", id: "G4", question: view.gaps[3].missing, why: view.gaps[3].why,
    who_can_answer: ["Ricardo (Quality)"], sides: [{side: "source records", citations: [DAMAGE]},
      {side: "paper original (machine-read; a person confirms)", citations: [PAGE3]}]}];
  view.proposals[0].approval = {state: "proposed", registered: true, intent_id: "act_1", owner_system: "portal", dispatched: false,
    dispatch_attempts: 0, gate: null, decisions: [], read_backs: [], human_preconditions: ["Quality confirms"], checked_preconditions: 1,
    chain_ok: true, where: "Mission Control", approved_here: false, note: "Recorded only. Needs human approval in Mission Control; the Workbench never approves, dispatches or executes it."};
  view.originals[0].source = {kind: "sharepoint-drive", path: "/AOs/2026 - AOs/AO-26005B.pdf", etag_status: "current", etag_reason: "the item's eTag read now equals the eTag at attach"};
  view.originals[0].integrity = {sha256_rehashed_on_this_read: true, status: "current"};
  return view;
}

test("fixture data is labelled fixture before anything else; unstated data is never called live", () => {
  const app = load();
  const fixture = app.caseEvidenceBanner(joinedView());
  assert.equal(fixture.fixture, true);
  assert.match(fixture.headline, /^FIXTURE DATA · isolated-fixture \/ injected-transport · not real PLX data$/);
  const root = app.renderCaseWorkspace(joinedView());
  assert.equal(root.children[0].className, "case-evidence fixture");
  assert.equal(root.children[0].attributes.role, "alert");
  assert.ok(content(root.children[0]).includes("FIXTURE DATA"));
  const unstated = app.caseEvidenceBanner({});
  assert.equal(unstated.fixture, false);
  assert.equal(unstated.headline, "Evidence: recorded case state");
  assert.ok(!/live/i.test(unstated.headline + unstated.note));
  // The case list says it too.
  vm.runInContext("state.cases = {available: true, cases: [], evidence: {label: 'isolated-fixture', fixture: true}}; state.caseView = null;", app);
  const target = new Element("div");
  app.casesView(target);
  assert.ok(content(target).includes("FIXTURE DATA"));
});

test("evidence conflicts are listed first with both sides cited, and leave the gap list", () => {
  const app = load();
  const view = joinedView();
  const groups = app.caseGapGroups(view);
  assert.deepEqual(plain(groups.map(group => [group.state, group.gaps.map(gap => gap.id)])), [["unknown", ["G1"]], ["absent_in_source", ["G2"]]]);
  const root = app.renderCaseWorkspace(view);
  const panels = walk(root).filter(node => node.className === "panel");
  const titles = panels.map(node => node.children[0].children[0].textContent);
  assert.equal(titles[0], "Conflicts");
  assert.ok(titles.indexOf("Conflicts") < titles.indexOf("Gaps"));
  const conflictPanel = panels[0];
  const chips = walk(conflictPanel).filter(node => node.tagName === "button").map(node => node.title);
  assert.deepEqual(plain(chips), [DAMAGE, PAGE3]);
  const text = content(conflictPanel);
  for (const part of ["evidence conflict", "shipper box damage", "record 20", "source records", "paper original"])
    assert.ok(text.includes(part), part);
  assert.ok(content(root).includes("1 conflicts"));
});

test("approval state comes from the action path and never claims a dispatch it did not record", () => {
  const app = load();
  const registered = app.caseApprovalSummary(joinedView().proposals[0].approval);
  assert.equal(registered.headline, "Awaiting human approval in Mission Control · proposed");
  assert.equal(registered.dispatched, false);
  assert.ok(registered.lines.includes("Dispatched: no"));
  assert.ok(registered.lines.includes("Mission Control gate: not requested yet"));
  assert.ok(registered.lines.includes("A person confirms: Quality confirms"));
  const none = app.caseApprovalSummary({state: "not_registered", dispatched: false, path: "not on the goal-2 action path"});
  assert.equal(none.headline, "Not requested · needs human approval in Mission Control");
  const authorized = app.caseApprovalSummary({...joinedView().proposals[0].approval, state: "gate_authorized",
    gate: {task_id: "TASK-1", gate_id: "apg_1"}, decisions: [{status: "approved", authorizes: true, decided_at: "t"}]});
  assert.equal(authorized.headline, "Approved in Mission Control · not dispatched");
  assert.ok(authorized.lines.includes("MC decision: approved (authorizes) at t"));
  const unknown = app.caseApprovalSummary({...joinedView().proposals[0].approval, state: "dispatched_unknown", dispatched: false,
    next_step: "read back the owning system; never re-send"});
  assert.equal(unknown.tone, "red");
  assert.ok(unknown.lines.includes("Next safe step: read back the owning system; never re-send"));
  const bounded = app.caseApprovalSummary({state: "unknown_scan_truncated", dispatched: null, detail: "only the newest 5000 action registrations were scanned"});
  assert.equal(bounded.headline, "Approval state unknown · action store scan bounded");
  assert.ok(bounded.lines.includes("Dispatched: unknown"));
  const withheld = app.caseApprovalSummary({state: "proposed", registered: true, withheld: true, dispatched: false});
  assert.equal(withheld.headline, "Withheld proposal · action path state proposed");
  assert.ok(!JSON.stringify(withheld).includes("undefined"));
  const text = content(app.renderCaseWorkspace(joinedView()));
  assert.ok(text.includes("Awaiting human approval in Mission Control · proposed"));
  assert.ok(text.includes("Action path state: proposed (intent act_1, owner portal)"));
});

test("originals show the re-hashed SHA-256, the SharePoint eTag status and page links", () => {
  const app = load();
  const original = joinedView().originals[0];
  const status = app.caseOriginalStatus(original);
  assert.match(status.sha.text, /^SHA-256 ffffffffffffffff… re-hashed on this read$/);
  assert.equal(status.etag.text, "SharePoint /AOs/2026 - AOs/AO-26005B.pdf · eTag unchanged since attach");
  assert.equal(app.caseOriginalStatus({...original, source: {...original.source, etag_status: "changed_since"}}).etag.tone, "amber");
  assert.equal(app.caseOriginalStatus({...original, source: {...original.source, etag_status: "not_rechecked"}}).etag.text,
    "SharePoint /AOs/2026 - AOs/AO-26005B.pdf · eTag not rechecked on this host");
  assert.equal(app.caseOriginalStatus({...original, source: {kind: "operator-inbox"}}).etag.text, "Operator inbox original (no source item to recheck)");
  const text = content(app.renderCaseWorkspace(joinedView()));
  for (const part of ["eTag unchanged since attach", "re-hashed on this read", "Page 1 · OCR", "Metadata proposals"])
    assert.ok(text.includes(part), part);
});

test("metadata proposals open through the audience-checked case route", async () => {
  const calls = [];
  const app = load(async url => { calls.push(url); return {ok: true, status: 200, json: async () => ({reading: "ocr_requires_review",
    portal_taxonomy: {columns: "cols@1"}, anchor: {status: "decided", entity_type: "ASSEMBLY_ORDER"}, linked_records: [], columns: []})}; });
  vm.runInContext("var opened = []; openDetail = node => opened.push(node);", app);
  const root = app.renderCaseWorkspace(joinedView());
  const button = walk(root).find(node => node.tagName === "button" && node.textContent === "Metadata proposals");
  await button.listeners.click();
  assert.equal(calls[0], `/api/case-document-metadata?handle=${encodeURIComponent(FILE)}`);
  const opened = vm.runInContext("opened", app);
  assert.ok(content(opened[0]).includes("Nothing is saved or written to SharePoint"));
});

test("case events carry the citations of the item they recorded on the timeline", () => {
  const app = load();
  const entries = app.caseTimeline(joinedView());
  const event = entries.find(entry => entry.ref === "F1");
  assert.deepEqual(plain(event.handles), [AO, ORDER]);
  const view = joinedView();
  view.source_dates.push({at: "2025-11-04T15:10:00Z", column: "received", relation: "mail", key: "Order confirmation", handle: "graph-mail://m/1@c#body_sha256=x"});
  const mail = app.caseTimeline(view).find(entry => entry.label === "mail · received");
  assert.equal(mail.detail, "mail message Order confirmation (as the mailbox records it)");
});

test("the joined view still offers no approve, execute, dispatch or send control", () => {
  const app = load();
  const buttons = walk(app.renderCaseWorkspace(joinedView())).filter(node => node.tagName === "button");
  assert.equal(buttons.filter(node => /approve|execute|dispatch|send/i.test(node.textContent)).length, 0);
});

test("action listing bounds and broken chains read unknown; a withheld intent never claims no dispatch it cannot know", () => {
  const app = load();
  const listed = app.caseApprovalSummary({state: "unknown_list_truncated", dispatched: null, detail: "only the first 200 of 250 action registrations of this case were listed"});
  assert.equal(listed.headline, "Approval state unknown · action listing bounded");
  assert.ok(listed.lines.includes("Dispatched: unknown"));
  const broken = app.caseApprovalSummary({state: "unknown_chain_broken", dispatched: null, detail: "1 action record(s) of this case fail their hash chain"});
  assert.equal(broken.tone, "red");
  assert.equal(broken.dispatched, null);
  const uncertain = app.caseApprovalSummary({state: "dispatched_unknown", registered: true, withheld: true, dispatched: null});
  assert.ok(uncertain.lines.includes("Dispatched: unknown"));
  assert.equal(uncertain.dispatched, null);
  const safe = app.caseApprovalSummary({...joinedView().proposals[0].approval, next_step: null,
    next_safe_step: {step: "request_gate", by: "host", detail: "a person approves the gate request"}});
  assert.ok(safe.lines.includes("Next safe step (host): a person approves the gate request"));
});


test("the session label names a live employee and its expiry, and a fixture as fixture", () => {
  const app = load();
  assert.equal(app.sessionLabel({fixture: false, employee: "employee:aaaaaaaa-0000-4000-8000-000000000001",
    author_kind: "human", expires_at: 1791000000}), "Signed in: employee …0001 · human · until Oct 3, 2026, 12:00 AM ET");
  assert.equal(app.sessionLabel({fixture: true, employee: null, author_kind: "human"}),
    "Fixture session · not a signed-in employee");
  assert.equal(app.sessionLabel(null), "No session identity");
});

// The status card (operating/status_card.py over /api/status-card), rendered by the
// object workspace's renderer: app.js and workspace.js in one context, as the page loads them.
function loadWithWorkspace(fetchImpl) {
  const context = load(fetchImpl);
  vm.runInContext(fs.readFileSync(path.join(__dirname, "workspace.js"), "utf8"), context);
  return context;
}
const PO_ROW = "api://public.app_purchase_order/%7B%22id%22%3A%22po-1%22%7D@g1-3";
const FM_ROW = "api://public.generated_purchase_order/%7B%22id%22%3A1%7D@g1-9";
function statusCard() {
  return {schema: "plx-status-card/v1", status: "read", object_id: "PurchaseOrder:x", object_type: "PurchaseOrder",
    type_label: "purchase order", label: {untrusted_text: "PO1001"}, as_of: "2026-10-20",
    headline: "2 things need attention",
    blockers: [{kind: "not_recorded", title: "No receipt found", why: "No receipt is recorded for it in what you can read; that is not proof there is none.",
      basis: "not_found", citations: [{handle: PO_ROW, field: "row.po_number"}], next_step: {who: ["Warehouse"], what: "Check whether it was received and record the receipt."}},
    {kind: "no_need_date", title: "No expected date recorded", why: "The expected date is empty on every copy.",
      basis: "proven", citations: [{handle: PO_ROW, field: "row.expected_date"}, {handle: FM_ROW, field: "row.datearrive"}],
      next_step: {who: ["Procurement"], what: "Ask the supplier for a delivery date and record it."}}],
    facts: [{text: "Purchase order in the Portal", value: "PO1001", citations: [{handle: PO_ROW, field: "row.po_number"}]}],
    copies: {text: "Also recorded in FileMaker.", count: 1, citations: [{handle: FM_ROW, field: "row.id"}]},
    unknowns: [{text: "The due date of the receipt is outside your access.", citations: []}],
    questions: [{family: "F14", title: "Which purchase orders are late, by how much and why"}], more_blockers: 0,
    next_step: {who: ["Warehouse"], what: "Check whether it was received and record the receipt."}};
}

test("a case shows its root's status card first: blockers, next steps, copies and unknowns, each cited", () => {
  const app = loadWithWorkspace();
  const view = {...caseView(), status_cards: [statusCard()]};
  const root = app.renderCaseWorkspace(view);
  assert.equal(root.children[0].className, "case-status-cards");
  const card = root.children[0].children[0];
  assert.equal(card.className, "ow-section status-card");
  const text = content(card);
  for (const part of ["Status · purchase order PO1001", "2 things need attention", "No receipt found",
    "Next: Warehouse: Check whether it was received", "Next: Procurement: Ask the supplier", "Also recorded in FileMaker.",
    "Not known", "The due date of the receipt is outside your access."])
    assert.ok(text.includes(part), `missing: ${part}`);
  // the case's own citation chips, each opening the cited record through the case route
  const cited = walk(card).filter(node => node.tagName === "button" && node.title).map(node => node.title);
  assert.deepEqual(plain(cited), [PO_ROW, PO_ROW, FM_ROW, PO_ROW, FM_ROW]);
  // Without a served card the case reads as before.
  assert.notEqual(app.renderCaseWorkspace(caseView()).children[0].className, "case-status-cards");
  // A card that did not read says so in one line, nothing else.
  const missing = app.renderCaseWorkspace({...caseView(), status_cards: [{status: "ambiguous", matches: 2}]}).children[0];
  assert.equal(content(missing).trim(), "No status card: more than one record matches; open one to see its card.");
});

test("a case asks for one status card per typed root, and an unserved card leaves the case as it was", async () => {
  const calls = [];
  const app = load(async url => {
    calls.push(url);
    if (url.includes("ref=PO2")) return {ok: false, status: 501, json: async () => ({error: "capability_unavailable"})};
    return {ok: true, status: 200, json: async () => statusCard()};
  });
  const cards = await app.loadCaseStatusCards({summary: {roots: [{id: "PO1001", type: "PurchaseOrder", label: "PO1001"},
    {id: "PO2", type: "PurchaseOrder", label: "PO2"}, {id: "untyped"}]}});
  assert.deepEqual(plain(calls), ["/api/status-card?type=PurchaseOrder&ref=PO1001", "/api/status-card?type=PurchaseOrder&ref=PO2"]);
  assert.equal(cards.length, 1);
  assert.deepEqual(plain(await app.loadCaseStatusCards({})), []);
});

test("the same gap recorded once per copy reads as one line with its copies and every citation", () => {
  const app = load();
  const gap = (id, handle) => ({id, state: "unknown", missing: "Receipt date", why: "Not recorded on this copy.",
    who_can_answer: ["Warehouse"], citations: [{handle, status: "resolved"}]});
  const view = {...caseView(), conflicts: [], gaps: [gap("G1", AO), gap("G2", ORDER), gap("G3", AO),
    {...gap("G4", ITEM), why: "A different reason."}]};
  const [group] = plain(app.caseGapGroups(view));
  assert.deepEqual(group.gaps.map(g => [g.ids, g.copies, g.citations.map(c => c.handle)]),
    [[["G1", "G2", "G3"], 3, [AO, ORDER]], [["G4"], 1, [ITEM]]]);
  const text = content(app.renderCaseWorkspace(view));
  assert.ok(text.includes("3 copies"));
  assert.equal(text.split("Not recorded on this copy.").length - 1, 1);
});
