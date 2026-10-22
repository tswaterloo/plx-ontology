"use strict";
// Employee object workspace, the console's landing view: start from a question (the
// objective box) or an identifier, find the served objects it names (plx.find, ranked,
// never merged), run a question family on one (plx.investigate: chain, cited answer,
// gaps, answerability) or open it as one view (plx.object card, quantities and money by
// role, plx.around map with why each link exists, plx.timeline, plx.process, gaps,
// conflicts, registered actions with their approval state, the originals: every cited
// capture with its version and SHA-256, and the mail, SharePoint and BC documents
// plx.documents answers for this reader). What is withheld from the reader is said where
// it would have been, never silently dropped. Each answer opens with its explanation
// (why, evidence you can open, gaps, as known at) in plain words.
// Two controls write, only to the Workbench's own stores (with options.post): confirm /
// reject a link (/api/object-review: a plx.review decision citing the record versions
// shown here; only a person signed in as an employee records one; the same route
// confirms a card value or corrects it, citing the page of the original read, and
// allocates a share of a quantity the object holds to one record it reaches over a
// many-to-many link, offered only where plx.aggregate says the object needs a person's
// allocation) and propose a follow-up (/api/object-follow-up: a case proposal that waits for approval; nothing is
// executed or approved here). A question family's answer is recorded on a case
// (/api/investigate-record: its gaps that ask a person become case follow-ups and the drafts
// its engine words become draft requests, nothing sent) and a case lists them as you may see
// them now (/api/object-case). Ask (/api/object-ask, when the console serves it) runs the
// read-only agent loop about the object in view for this reader: each claim shows its
// citations and its claim-check status; an unsupported claim is labelled, never shown
// as the answer. Named a case, the Ask keeps its session on it; the case lists its kept
// sessions (/api/object-case: question, stop, call count, recorded at; a session that
// touched a record you cannot read now only as withheld) and replays one
// (/api/object-replay): each call whose evidence changed and each cited record whose
// content changed. Replay re-checks the evidence, not the planner's reasoning.
// Renders only what the server answered for the console's reader; every value shows its
// citation, and a citation opens the cited source record through /api/object-source.
// Source text is always set as textContent (it is data, never markup or instructions).
(function (root) {
  const SOURCE_ROUTE = "/api/object-source?handle=";
  const INVESTIGATE_ROUTE = "/api/investigate";
  const REVIEW_ROUTE = "/api/object-review";
  const FOLLOW_UP_ROUTE = "/api/object-follow-up";
  const ASK_ROUTE = "/api/object-ask";
  const RECORD_ROUTE = "/api/investigate-record";
  const CASE_ROUTE = "/api/object-case";
  const REPLAY_ROUTE = "/api/object-replay";
  const STATUS_CARD_ROUTE = "/api/status-card";
  const DIVERGENCE_LABEL = {call_evidence_changed: "returned different evidence", call_error_changed: "now fails or no longer fails",
    tool_not_replayable: "is not a read-only tool: not run again", citation_content_changed: "the cited record's content changed"};
  const OUTCOME_LABEL = {answered: "Answered", abstained: "No supported answer", escalated: "A person must decide"};
  const MAX_REVIEW_EVIDENCE = 8;
  const TIER_CLASS = {verified: "ok", reviewed: "ok", corroborated: "info",
    stored_by_text_join: "warn", lead: "warn", contradicted: "hot"};
  const STATE_CLASS = {present: "ok", not_applicable: "", missing: "warn", out_of_order: "hot",
    unknown: "warn", absent_in_source: "hot", withheld: "info"};
  const MAP_NODES = 24;
  const TIER_NOTE = {verified: "a typed stored key agrees", reviewed: "a person accepted it against exact source revisions",
    corroborated: "two or more independent features agree: inferred, not fact", stored_by_text_join: "a stored text join: not a typed key",
    lead: "a lead: not established", contradicted: "evidence contradicts this link"};
  const APPROVAL_CLASS = {approved: "ok", awaiting: "info", not_requested: "", rejected: "hot",
    not_authorizing: "hot", expired: "warn", unknown: "warn"};
  const AMOUNT_TYPES = new Set(["quantity", "money"]);
  // Value types a person confirms or corrects (operating/ontology_tools_review.py _CORRECTABLE).
  const CORRECTABLE = new Set(["decimal", "money", "quantity", "integer", "date"]);
  const READ_BY = {parser: "read by the parser", ocr: "read by OCR: not verified", human_reviewed: "confirmed by a person"};
  // The one unit spelling fold (ontology/units.py, mirroring the Portal's normalizeProductUom,
  // product-uom.ts@fa46f2d); ontology/tests/test_units.py fails when these tables differ.
  const UOM_CANONICAL = ["ea", "cse", "dz", "box", "bag", "bottle", "drum", "pail", "roll", "pack", "kg", "g", "mg", "lb", "oz", "l", "ml", "gal"];
  const UOM_ALIASES = {"each": "ea", "eac": "ea", "unit": "ea", "units": "ea", "pc": "ea", "pcs": "ea", "ech": "ea", "case": "cse", "cases": "cse", "cs": "cse", "dozen": "dz", "doz": "dz", "boxes": "box", "packs": "pack", "btl": "bottle", "drm": "drum", "kgs": "kg", "kilogram": "kg", "kilograms": "kg", "gram": "g", "grams": "g", "lbs": "lb", "pound": "lb", "pounds": "lb", "litre": "l", "liter": "l", "lt": "l"};
  // The Portal's receiving unitsCompatible plurals (apply-po-line-receive.ts@6e76a5b:46-48).
  const RECEIVE_ALIASES = {"bags": "bag", "drums": "drum"};
  const UOM_CANONICAL_SET = new Set(UOM_CANONICAL);

  // A unit as the Portal folds its spelling: trimmed, lower-cased, an alias mapped to its
  // option, a receiving plural to its singular, any other token itself; null when empty.
  // Never a conversion.
  function unitToken(raw) {
    if (typeof raw !== "string") return null;
    let token = raw.trim().toLowerCase();
    if (!token) return null;
    if (UOM_CANONICAL_SET.has(token)) return token;
    const own = Object.prototype.hasOwnProperty;
    if (own.call(UOM_ALIASES, token)) token = UOM_ALIASES[token];
    return own.call(RECEIVE_ALIASES, token) ? RECEIVE_ALIASES[token] : token;
  }

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
  function list(v) { return Array.isArray(v) ? v : []; }
  function words(text) { return String(text || "").replace(/_/g, " "); }

  // Times as app.js shows them: an instant in America/New_York, labelled ET, to the
  // minute; a date-only source value names a day and stays as written; a time without a
  // zone says so. The raw value stays in the datetime and title attributes.
  const ET_PARTS = typeof Intl !== "undefined" ? new Intl.DateTimeFormat("en-US", {timeZone: "America/New_York",
    year: "numeric", month: "short", day: "numeric", hour: "numeric", minute: "2-digit", hour12: true}) : null;
  function etTime(text) {
    const raw = String(text === null || text === undefined ? "" : text).trim();
    if (!raw) return "—";
    const match = /^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2})(?::\d{2}(?:\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?$/i.exec(raw);
    if (!match || !ET_PARTS) return raw;
    if (!match[3]) return `${match[1]} ${match[2]} (time zone not stated)`;
    const at = new Date(raw);
    if (Number.isNaN(at.getTime())) return raw;
    const part = {};
    ET_PARTS.formatToParts(at).forEach(item => { part[item.type] = item.value; });
    return `${part.month} ${part.day}, ${part.year}, ${part.hour}:${part.minute} ${String(part.dayPeriod || "").toUpperCase()} ET`;
  }
  function timeNode(d, text) {
    const node = d.el("time", "", etTime(text));
    if (text) { node.setAttribute("datetime", String(text)); node.setAttribute("title", String(text)); }
    return node;
  }

  // A lineage (how a value reached the store, ontology/store_ddl.py LINEAGES) in an
  // employee's words; the code stays in a title attribute. An unknown lineage is "copy".
  const LINEAGE_WORDS = {fm_bronze: "FileMaker capture", sage_bronze: "Sage capture", silver_view: "cleaned view",
    gold_etl_from_fm: "FileMaker copy", gold_etl_from_sage: "Sage copy", gold_native_staging: "Portal record",
    gold_so_bridge: "order bridge", portal_api: "Portal record", workbench: "a person's review", graph_mail: "mail"};
  function lineageWords(lineage) {
    const text = String(lineage || "").trim();
    if (!text) return "";
    return text.split(/\s*,\s*/).map(item => Object.prototype.hasOwnProperty.call(LINEAGE_WORDS, item) ? LINEAGE_WORDS[item]
      : /^bc_/.test(item) ? "Business Central record" : "source copy").filter((v, i, all) => all.indexOf(v) === i).join(", ");
  }

  // An engineer's detail: the raw codes, paths and identifiers behind a plain sentence.
  // Closed by default; the walkthrough treats only text inside details.engineer as code.
  function engineerNode(d, summary, lines) {
    const items = list(lines).filter(line => line !== null && line !== undefined && String(line).trim());
    if (!items.length) return null;
    const box = d.el("details", "engineer");
    d.add(box, d.el("summary", "", summary || "Engineer detail"), ...items.map(line => d.el("code", "", String(line))));
    return box;
  }
  // Server text that names code (a file path, a relation or tool name, a staging
  // lineage, a link's ordinal) is an engineer's text: it goes into an engineer detail. Other
  // server prose is said in words: a link type (Type.relation, with its direction) in
  // words, a snake_case word with spaces.
  const ENGINEER_TEXT = /\.\*\.|\b[a-z]+:[A-Za-z0-9_-]{16,}|[\w-]+\.(?:ts|tsx|mjs|js|py|sql)\b|\b[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*\b|gold_native_staging|never_authority|#\d+\b|:\/\//;
  const LINK_TOKEN = /\b([A-Z][A-Za-z0-9]*)\.([a-z0-9_]+)(?:\s+(forward|reverse))?\b/g;
  function typeWords(type) { return String(type || "").replace(/([a-z0-9])([A-Z])/g, "$1 $2").toLowerCase(); }
  // "Header.ordered_from forward" -> "ordered from"; "Line.line_of reverse" -> "line (line of)".
  function linkWords(raw) {
    const text = String(raw || "");
    if (text === "object") return "this record";
    return text.replace(LINK_TOKEN, (all, type, relation, direction) =>
      direction === "reverse" ? `${typeWords(type)} (${words(relation)})` : words(relation));
  }
  // A reason code (PO_LINES_INCOMPLETE) reads as lower-case words; a code identifier the
  // server cites in brackets (a function name) is dropped. The raw text stays in a title.
  function plainText(text) {
    return linkWords(humanize(String(text === null || text === undefined ? "" : text)))
      .replace(/\s*\((?:[a-z]+[A-Z][A-Za-z0-9]*)\)/g, "")
      .replace(/\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b/g, m => words(m.toLowerCase()))
      .replace(/\b([a-z][a-z0-9]*(?:_[a-z0-9]+)+)\b/g, m => words(m));
  }
  // A searched source: a source system by name, a Type.field as "purchase order line unit price".
  function sourceWords(source) {
    const text = String(source || "");
    if (SOURCE_WORDS[text]) return SOURCE_WORDS[text];
    const field = /^([A-Z][A-Za-z0-9]*)\.([a-z][a-z0-9_]*)$/.exec(text);
    return field ? `${typeWords(field[1])} ${words(field[2])}` : words(text);
  }
  function plainOrEngineer(d, cls, text, summary) {
    const said = String(text === null || text === undefined ? "" : text);
    if (!said.trim()) return null;
    // Object ids the server labelled read as words first; what is still code after that
    // (a field path, a source address) goes behind the engineer toggle.
    if (ENGINEER_TEXT.test(humanize(said))) return engineerNode(d, summary || "Engineer detail", [said]);
    const node = d.el("span", cls, plainText(said));
    if (plainText(said) !== said) node.setAttribute("title", said);
    return node;
  }
  const SOURCE_WORDS = {portal_api: "the Portal", graph_mail: "mail", sharepoint: "SharePoint", bc: "Business Central",
    original: "attached originals", fm: "FileMaker", sage: "Sage"};
  function searchedNodes(d, searched) {
    if (!searched) return [];
    const sources = list(searched.sources);
    const box = d.el("small", "ow-muted");
    d.add(box, d.el("span", "", "searched " + (sources.map(sourceWords).join(", ") || "no source")),
      searched.since ? d.el("span", "", " since ") : null, searched.since ? timeNode(d, searched.since) : null);
    return [box, engineerNode(d, "Engineer detail", [`searched ${sources.join(", ")}${searched.since ? " since " + searched.since : ""}`])];
  }


  function sourceHref(handle) { return SOURCE_ROUTE + encodeURIComponent(handle); }

  // An untrusted_text envelope -> its text; other values -> display text.
  function valueText(value) {
    if (value && typeof value === "object" && typeof value.untrusted_text === "string")
      return value.untrusted_text + (value.truncated ? " …" : "");
    if (value === null || value === undefined) return null;
    if (typeof value === "boolean") return value ? "yes" : "no";
    return String(value);
  }

  // An object id (Type:source.relation:{key}) in words is the server's wording
  // (operating/explanations.py object_labels, sent as explanation.labels): the client looks
  // an id up and never rebuilds the words. An id the server did not label stays as it is,
  // and so does anything that is no object id. The raw id stays in a title attribute.
  const OBJECT_ID = /\b([A-Z][A-Za-z0-9]*):[a-z0-9_]+(?:\.[a-z0-9_]+)+:(\{[^{}]*\}|"[^"]*"|-?\d+(?:\.\d+)?)/g;
  let serverLabels = {};
  function useLabels(data) {
    const labels = data && data.explanation && data.explanation.labels;
    serverLabels = labels && typeof labels === "object" ? labels : {};
  }
  function labelOf(objectId) {
    const text = String(objectId || "");
    return Object.prototype.hasOwnProperty.call(serverLabels, text) ? String(serverLabels[text]) : text;
  }
  // Server prose (a gap's subject or detail, a conflict's subject) with each object id in
  // it named by the server's label. Only server-worded text: source text is never rewritten.
  function humanize(text) {
    if (text === null || text === undefined) return text;
    return String(text).replace(OBJECT_ID, id => labelOf(id));
  }

  function displayOf(object) {
    const text = object ? valueText(object.display) : null;
    return text || (object && object.object_id ? labelOf(object.object_id) : "object");
  }

  // A refusal or failure in plain words (app.js api/post throw the server's detail, else
  // its code): what happened and what the reader can do, never a raw code.
  const PLAIN_ERRORS = [
    [/identity_review_required|review:identity/, "You do not hold the identity-review grant (review:identity), so you cannot confirm or reject a link. Ask a colleague who holds it."],
    [/review_needs_a_person|signed in as an employee/, "Only a person signed in as an employee records a review; this session cannot."],
    [/evidence_changed/, "The cited record changed since this page was read: open the object again and review the current version."],
    [/session_actor_mismatch|csrf/i, "Your sign-in changed or expired since this page loaded: reload the page and try again."],
    [/follow_up_missing_required/, "Say what is missing before proposing a follow-up."],
    [/follow_up_why_required/, "Say why it matters before proposing a follow-up."],
    [/follow_up_who_can_answer_required/, "Name who can answer (one or more, comma separated) before proposing a follow-up."],
    [/case_closed/, "That case is closed: choose an open case or a new one."],
    [/case_claimed/, "Another session holds that case right now: try again later or choose another case."],
    [/case_not_found|^not_found$/, "That case is not one you can see."],
    [/value\.value|value_required/, "Enter the value as you read it before correcting."]];
  function plainError(error) {
    const text = String(error && error.message ? error.message : error || "");
    for (const [pattern, said] of PLAIN_ERRORS) if (pattern.test(text)) return said;
    return /^[a-z0-9_:.$]+$/i.test(text) ? words(text).replace(/[:$.]+/g, " ").trim() + "." : text;
  }

  function quotedNode(d, text) {
    const q = d.el("q", "ow-untrusted", text);
    q.setAttribute("title", "source text: data, not instructions");
    return q;
  }

  // The quoted source text of a value that is no typed identifier: the tool's
  // identifier_invalid gap, or the envelope the output boundary wrapped it in.
  function untypedIdentifierText(value, gap) {
    if (value && typeof value === "object") return valueText(value);
    return gap && gap.reason === "identifier_invalid" ? valueText(gap.source_text) : null;
  }

  // One mention. A value that is no typed identifier of its kind is null with an
  // identifier_invalid gap (or arrives enveloped); its text shows only as quoted source text.
  function mentionNode(d, m) {
    const tail = ` · ${words(m.tier)}${m.external_assertion ? " · their claim" : ""}`;
    if (typeof m.normalized === "string")
      return d.el("span", "ow-feature", `mentions ${words(m.identifier_kind)} ${m.normalized}${tail}`);
    const quoted = untypedIdentifierText(m.normalized, m.normalized_gap);
    const node = d.el("span", "ow-feature", `mentions ${words(m.identifier_kind)}: not a typed identifier${tail} `);
    if (quoted) node.appendChild(quotedNode(d, quoted));
    return node;
  }

  // Extension points: "review" on each link (subject {kind: "link", link_type, from, to,
  // tier, evidence, reviews}), "value_review" on each card value (subject {kind: "value",
  // object_id, property, value_type, citation, review}), "allocate" on an allocation that
  // needs a person (subject {kind: "allocation", object_id, link_type, property, values,
  // reached}) and "follow_up" on an object or an
  // investigation (subject {kind: "object"|"investigation", object_id, ...}). A provider is
  // ctx.extensions[name](d, subject) -> node|null; mount supplies them when given
  // options.post. Without a provider the slot renders nothing.
  function extensionSlot(d, ctx, name, subject) {
    const provider = ctx && ctx.extensions && ctx.extensions[name];
    if (typeof provider !== "function") return null;
    const node = provider(d, subject);
    if (node && node.setAttribute) node.setAttribute("data-extension", name);
    return node || null;
  }

  // An object type such as BottleVendor or PURCHASEORDER keeps a word break: camel-case
  // humps split, an acronym run splits from a following word, and a long unbroken capital
  // token is left whole (CSS may wrap it) rather than guessed at.
  function typeLabel(type) {
    return String(type || "").replace(/_/g, " ").replace(/([a-z0-9])([A-Z])/g, "$1 $2").replace(/([A-Z]+)([A-Z][a-z])/g, "$1 $2");
  }
  const TIER_ORDER = ["verified", "reviewed", "corroborated", "stored_by_text_join", "lead", "contradicted"];
  // The weakest tier among the links actually shown, not the floor requested of the server.
  function weakestShown(hops) {
    let weakest = -1;
    let unknown = null;
    list(hops).forEach(hop => list(hop && hop.result && hop.result.edges).forEach(edge => {
      const at = TIER_ORDER.indexOf(edge.tier);
      if (at < 0) unknown = String(edge.tier || "unknown");
      else weakest = Math.max(weakest, at);
    }));
    if (unknown) return unknown;
    return weakest < 0 ? "no links" : TIER_ORDER[weakest];
  }
  function tagNode(d, text, cls) { return d.el("span", "ow-tag" + (cls ? " ow-" + cls : ""), words(text)); }

  // One citation: a link that opens the cited record for this reader (or a static
  // reference when the console has no source reader). Handle, field path and lineage show.
  // A cited field in words ("row.custom_fields.fmGeneratedPoId" -> "custom fields fm generated
  // po id"); the path itself stays in the citation's title.
  function fieldWords(path) {
    const parts = String(path || "").split(".").filter(Boolean);
    if (parts[0] === "row" && parts.length > 1) parts.shift();
    const said = parts.map(part => part === "*" ? "each" : words(typeWords(part))).join(" ").trim();
    return said || "record";
  }
  function citationNode(d, citation, ctx) {
    if (!citation || !citation.l1_handle) return d.el("span", "ow-cite ow-cite-missing", "no citation");
    const from = lineageWords(citation.lineage);
    const label = fieldWords(citation.field_path) + (from ? " · " + from : "");
    let node;
    if (ctx && ctx.openable) {
      node = d.el("a", "ow-cite", "source ↗ " + label);
      node.setAttribute("href", sourceHref(citation.l1_handle));
      node.setAttribute("data-handle", citation.l1_handle);
      node.addEventListener("click", event => {
        if (event && event.preventDefault) event.preventDefault();
        if (ctx.onSource) ctx.onSource(citation.l1_handle);
      });
    } else {
      node = d.el("span", "ow-cite ow-cite-static", "cited " + label);
    }
    node.setAttribute("title", citation.l1_handle + (citation.field_path ? "\nfield " + citation.field_path : "") + (citation.lineage ? "\nlineage " + citation.lineage : "") + (citation.digest ? "\ndigest " + citation.digest : ""));
    return node;
  }

  // A stored identifier that is no typed identifier of its kind (a customer PO field
  // holding a name) is null with an identifier_invalid value_gap (any other value that
  // does not fit its type: value_invalid): it is not empty in the source, so it says so
  // and quotes the text.
  function valueNode(d, value) {
    const text = valueText(value.value);
    const box = d.el("span", "ow-value");
    const gap = value.value_gap;
    const invalid = value.value_type === "identifier" ? untypedIdentifierText(value.value, gap)
      : text === null && gap && gap.reason === "value_invalid" ? valueText(gap.source_text) : null;
    if (invalid !== null) {
      d.add(box, d.el("span", "ow-warn", value.value_type === "identifier" ? "not a typed identifier" : "not a typed value"),
        quotedNode(d, invalid));
    } else if (text === null) {
      d.add(box, d.el("span", "ow-empty", "empty in source"));
    } else if (value.value && typeof value.value === "object") {
      d.add(box, quotedNode(d, text));
    } else {
      d.add(box, d.el("span", "ow-typed", text));
    }
    // An empty value has no unit or currency to be unknown.
    const empty = text === null && invalid === null;
    if (value.unit) d.add(box, d.el("span", "ow-unit", value.unit));
    else if (value.unit_gap && !empty) d.add(box, d.el("span", "ow-unit ow-warn", "unit unknown"));
    if (value.currency) d.add(box, d.el("span", "ow-unit", value.currency));
    else if (value.currency_gap && !empty) d.add(box, d.el("span", "ow-unit ow-warn", "currency unknown"));
    if (value.quantity_role) d.add(box, tagNode(d, value.quantity_role));
    return box;
  }

  function markingsNode(d, markings) {
    const items = list(markings);
    return items.length ? d.el("span", "ow-markings", "marked " + items.join(", ")) : null;
  }

  function withheldNode(d, withheld) {
    if (!withheld) return null;
    const parts = [];
    if (withheld.properties) parts.push(`${withheld.properties} propert${withheld.properties === 1 ? "y" : "ies"}`);
    if (withheld.objects) parts.push(`${withheld.objects} object${withheld.objects === 1 ? "" : "s"}`);
    const reason = words(withheld.reason || "policy");
    if (parts.length) return d.el("p", "ow-withheld", `Withheld from you: ${parts.join(" and ")} (${reason}).`);
    if (withheld.properties === null || withheld.objects === null)
      return d.el("p", "ow-withheld", `Parts may be withheld from you (${reason}); the policy does not reveal how many.`);
    return null;
  }

  function completenessNode(d, completeness) {
    if (!completeness || completeness.complete) return null;
    return d.el("p", "ow-partial", `Partial: ${words(completeness.reason || "not complete")}` +
      (completeness.truncated ? ` (showing ${completeness.returned}${completeness.total != null ? " of " + completeness.total : ""})` : ""));
  }

  function section(d, title, hint, id) {
    const n = d.el("section", "ow-section");
    if (id) n.setAttribute("data-section", id);
    const head = d.el("div", "ow-section-head");
    d.add(head, d.el("h2", "", title), hint ? d.el("small", "", hint) : null);
    d.add(n, head);
    return n;
  }

  function errorNode(d, error) {
    return d.el("p", "ow-error", `${words(error.code || "error")}: ${error.message || ""}`);
  }

  // ---- the explanation block (operating/explanations.py) ---------------------------
  // Every answer says, in words: why (the rule, plan step or link reason behind it), the
  // evidence the reader may open, what is missing, disputed or withheld, and the capture
  // it reflects (stale during an outage). Handles stay in title attributes, never the text.

  const GAP_CLASS = {missing: ["Not established", "warn"], conflicting: ["Sources disagree", "hot"],
    withheld: ["Withheld for your access", "info"], incomplete_source: ["Source incomplete", "warn"],
    upstream_outage: ["Source unavailable", "hot"]};

  function explanationSection(d, explanation, ctx) {
    if (!explanation) return null;
    const s = section(d, "Why this answer", "why, the evidence you can open, gaps, and as known at", "explanation");
    const known = explanation.as_known_at || {};
    s.appendChild(d.el("p", "ow-known ow-known-" + (known.state || "unknown"), known.label || "Capture time not stated"));
    const why = d.el("ul", "ow-explain-why");
    list(explanation.why).forEach(item => {
      const li = d.el("li", "", humanize(item.text));
      const value = valueText(item.value);
      if (value !== null) li.appendChild(item.value && typeof item.value === "object" ? quotedNode(d, value)
        : d.el("span", "ow-typed", [value, item.unit, item.currency].filter(Boolean).join(" ")));
      if (list(item.steps).length) li.appendChild(d.el("small", "ow-muted", item.steps.join(" · ")));
      why.appendChild(li);
    });
    if (!list(explanation.why).length) why.appendChild(d.el("li", "ow-muted", "No rule or link was reported for this answer."));
    const evidence = list(explanation.evidence);
    const total = explanation.evidence_total || evidence.length;
    d.add(s, d.el("h3", "", "Why"), why,
      d.el("h3", "", `Evidence you can open (${evidence.length < total ? evidence.length + " of " + total : total})`));
    evidence.forEach(item => {
      const row = d.el("div", "ow-evidence");
      let link;
      if (ctx && ctx.openable && item.openable) {
        link = d.el("a", "ow-cite", item.label + " ↗");
        link.setAttribute("href", sourceHref(item.handle));
        link.addEventListener("click", event => {
          if (event && event.preventDefault) event.preventDefault();
          if (ctx.onSource) ctx.onSource(item.handle);
        });
      } else {
        link = d.el("span", "ow-cite ow-cite-static", item.label + " (not openable here)");
      }
      link.setAttribute("title", item.handle || "");
      d.add(row, link, item.title ? quotedNode(d, valueText(item.title)) : null,
        list(item.fields).length ? d.el("small", "ow-muted", "fields: " + item.fields.join(", ")) : null,
        list(item.cited_for).length ? d.el("small", "ow-muted", "supports: " + item.cited_for.join(", ")) : null);
      s.appendChild(row);
    });
    if (!evidence.length) s.appendChild(d.el("p", "ow-muted", "No cited record of this answer is open to you."));
    s.appendChild(d.el("h3", "", "What is missing, disputed or withheld"));
    list(explanation.gaps).forEach(gap => {
      const [title, cls] = GAP_CLASS[gap.class] || ["Gap", "warn"];
      const row = d.el("div", `ow-gap ow-explain-gap ow-state-${cls}`);
      row.setAttribute("data-gap-class", gap.class || "");
      d.add(row, tagNode(d, title, cls), d.el("strong", "", humanize(gap.text) + (gap.count > 1 ? ` (${gap.count})` : "")),
        list(gap.where).length ? d.el("small", "ow-muted", "in " + gap.where.join(", ")) : null,
        list(gap.about).length ? d.el("span", "ow-gap-subject", "about " + gap.about.join("; ")) : null);
      list(gap.details).forEach(detail => row.appendChild(d.el("span", "ow-reason", humanize(detail))));
      s.appendChild(row);
    });
    if (!list(explanation.gaps).length) s.appendChild(d.el("p", "ow-muted", "No gap was reported for this answer."));
    return s;
  }

  // ---- find --------------------------------------------------------------------

  function renderFind(doc, data, ctx) {
    const d = dom(doc);
    useLabels(data);
    const box = d.el("div", "ow-results");
    const result = data && data.find;
    if (!result) return box;
    if (result.error) return d.add(box, errorNode(d, result.error));
    const status = result.status === "ambiguous"
      ? `${list(result.candidates).length} candidates: none is picked for you`
      : result.status === "found" ? "1 match" : "no visible match";
    d.add(box, d.el("p", "ow-find-status", status));
    const plans = (data && data.plans) || {};
    list(result.candidates).forEach(candidate => {
      const object = candidate.object || {};
      const holder = d.el("div", "ow-candidate-row");
      holder.setAttribute("data-object-id", object.object_id || "");
      const row = d.el("button", "ow-candidate");
      row.type = "button";
      row.setAttribute("data-object-id", object.object_id || "");
      const matched = candidate.matched || {};
      d.add(row, candidate.rank != null ? d.el("span", "ow-rank", "#" + candidate.rank) : null,
        d.el("span", "ow-candidate-title", displayOf(object)),
        tagNode(d, typeLabel(object.object_type)), tagNode(d, candidate.tier, TIER_CLASS[candidate.tier]),
        d.el("span", "ow-candidate-sub",
          `${words(candidate.reason)} · ${words(matched.identifier_kind || "")} ${matched.normalized || ""}${object.lineage ? " · " + lineageWords(object.lineage) : ""}`));
      row.setAttribute("title", `via ${matched.binding || "?"} · lineage ${object.lineage || "?"}`);
      row.addEventListener("click", () => ctx.onOpen && ctx.onOpen(object.object_id));
      d.add(holder, row, plansNode(d, plans[object.object_type], object.object_id, ctx));
      box.appendChild(holder);
    });
    list(result.gaps).forEach(gap => box.appendChild(gapNode(d, gap)));
    d.add(box, withheldNode(d, result.withheld), completenessNode(d, result.completeness),
      technicalDetail(d, [explanationSection(d, data.explanation, ctx)]));
    return box;
  }

  // The question families (ontology/plans) the server lists for a candidate's type.
  function plansNode(d, plans, objectId, ctx) {
    const items = list(plans);
    if (!items.length) return null;
    const box = d.el("div", "ow-plans");
    d.add(box, d.el("span", "ow-why-label", "Answer with:"));
    items.forEach(plan => {
      const button = d.el("button", "ow-link-button ow-plan", plan.title || plan.family);
      button.type = "button";
      button.setAttribute("data-family", plan.family || "");
      button.setAttribute("title", plan.description || "");
      button.addEventListener("click", () => ctx.onInvestigate && ctx.onInvestigate(plan.family, objectId));
      box.appendChild(button);
    });
    return box;
  }

  // ---- one question family (plx.investigate) ------------------------------------------

  function answerNode(d, entry, ctx) {
    const row = d.el("div", "ow-value-row");
    row.setAttribute("data-answer", entry.name || "");
    let shown;
    if (entry.value === null || entry.value === undefined) {
      shown = d.el("span", "ow-warn", "not established (see the gaps)");
    } else if (entry.kind === "count") {
      shown = d.el("span", "ow-typed", `${entry.value} ${entry.object_type || ""} visible to you`);
    } else {
      shown = valueNode(d, entry);
    }
    // One answer per row: its name, its value, then what backs it (citation, claim check,
    // lead) in one wrapping strip, so values never run into each other.
    const meta = d.el("span", "ow-value-meta");
    d.add(meta, entry.citation ? citationNode(d, entry.citation, ctx) : null,
      entry.check && entry.check !== "not_applicable" ? tagNode(d, "claim " + entry.check, entry.check === "passed" ? "ok" : "warn") : null,
      entry.lead_basis ? tagNode(d, "rests on a lead", "warn") : null);
    if (entry.object_id) {
      const open = d.el("button", "ow-link-button", "open");
      open.type = "button";
      open.setAttribute("data-object-id", entry.object_id);
      open.addEventListener("click", () => ctx.onOpen && ctx.onOpen(entry.object_id));
      meta.appendChild(open);
    }
    d.add(row, d.el("span", "ow-prop", words(entry.name)), shown, meta.children.length ? meta : null);
    return row;
  }

  function renderInvestigation(doc, data, ctx) {
    const d = dom(doc);
    useLabels(data);
    const box = d.el("div", "ow-investigation");
    box.setAttribute("data-family", data.family || "");
    d.add(box, banner(d, data));
    const result = data.investigation || {};
    const s = section(d, "Answer", words(data.family || ""), "investigation");
    if (result.procedure) s.setAttribute("title", "procedure " + result.procedure);
    box.appendChild(s);
    const technical = technicalDetail(d, [explanationSection(d, data.explanation, ctx)]);
    if (result.error) { d.add(s, errorNode(d, result.error)); d.add(box, technical); return box; }
    const answerability = result.answerability || {state: "unknown"};
    const cls = answerability.state === "answerable" ? "ok" : answerability.state === "blocked" ? "hot" : "warn";
    const head = d.el("p", "ow-answerability");
    const reasons = list(answerability.reasons).map(words);
    d.add(head, tagNode(d, ANSWERABILITY_TAG[answerability.state] || answerability.state, cls),
      d.el("span", "", " " + (ANSWERABILITY_WORDS[answerability.state] || `Plan status: ${words(answerability.state)}.`)),
      reasons.length ? d.el("span", "ow-reason", ` Not established yet: ${reasons.join(", ")}.`) : null);
    s.appendChild(head);
    if (result.root) {
      const open = d.el("button", "ow-link-button", "Root: " + displayOf(result.root));
      open.type = "button";
      open.setAttribute("data-object-id", result.root.object_id || "");
      open.addEventListener("click", () => ctx.onOpen && ctx.onOpen(result.root.object_id));
      s.appendChild(open);
    }
    if (list(result.candidates).length) s.appendChild(d.el("p", "ow-partial", `${result.candidates.length} roots match: none is picked for you; open one and ask again.`));
    const answers = d.el("div", "ow-values");
    list(result.answer).forEach(entry => answers.appendChild(answerNode(d, entry, ctx)));
    if (!list(result.answer).length) answers.appendChild(d.el("p", "ow-muted", "No answer entry was established for you."));
    s.appendChild(answers);
    const claims = result.claim_checks;
    // An answer entry the check could not verify (no role contract) is neither passed nor
    // failed: the line counts it, never "all passed".
    const unverified = list(result.answer).filter(entry => entry && entry.check === "unverified").length;
    const failed = claims && !claims.passed ? list(claims.failures).length : 0;
    const verdict = [failed ? `${failed} failed` : "", unverified ? `${unverified} unverified` : ""].filter(Boolean).join(", ");
    if (claims && claims.checked) s.appendChild(d.el("p", !claims.passed ? "ow-error" : unverified ? "ow-partial" : "ow-muted",
      `Claim checks: ${claims.claims} claim(s) re-read against their cited rows, ${verdict || (claims.passed ? "all passed" : "0 failed")}.`));
    list(result.conflicts).forEach(conflict => s.appendChild(d.el("p", "ow-conflict", `${words(conflict.kind || "conflict")}: ${humanize(conflict.subject || "")}${conflict.note ? " · " + humanize(conflict.note) : ""}`)));
    list(result.gaps).forEach(gap => s.appendChild(gapNode(d, gap)));
    d.add(s, withheldNode(d, result.withheld), completenessNode(d, result.completeness));
    const chain = list((result.chain || {}).objects);
    if (chain.length) {
      const reached = d.el("div", "ow-chain");
      const items = d.el("ul", "ow-chain-list");
      d.add(reached, d.el("h3", "", `Records reached (${chain.length})`), items);
      chain.forEach(object => {
        const li = d.el("li", "ow-chain-item");
        const open = d.el("button", "ow-link-button", displayOf(object));
        open.type = "button";
        open.setAttribute("data-object-id", object.object_id || "");
        open.addEventListener("click", () => ctx.onOpen && ctx.onOpen(object.object_id));
        d.add(li, open, object.object_type ? d.el("small", "ow-muted", typeWords(object.object_type)) : null);
        items.appendChild(li);
      });
      s.appendChild(reached);
    }
    d.add(s, extensionSlot(d, ctx, "follow_up", {kind: "investigation", object_id: data.root, family: data.family,
      procedure: result.procedure, answerability: answerability.state, case_id: ctx.caseId || null}),
      extensionSlot(d, ctx, "record_on_case", {kind: "investigation", object_id: data.root, family: data.family}));
    d.add(box, technical);
    return box;
  }
  // A plan's status in words (plx.investigate answerability).
  const ANSWERABILITY_TAG = {answerable: "answered", partial: "partly answered", blocked: "cannot be answered", unknown: "status unknown"};
  const ANSWERABILITY_WORDS = {
    answerable: "Plan status: answered. Every part of this question is established from records you may read.",
    partial: "Plan status: partly answered. Some parts are established; the rest are not established (see the gaps).",
    blocked: "Plan status: cannot be answered from the records you may read (see the gaps).",
    unknown: "Plan status: not reported."};

  // Record on a case (POST /api/investigate-record): the server runs the question family again
  // for this session's reader and records the answer's gaps that ask a person as case
  // follow-ups and the drafts its engine words (a freight quote request ...), each citing what
  // the answer showed you; nothing is sent. Empty case id: a case opened for this answer (a
  // second press records nothing twice). "Show case" reads a case's follow-ups and drafts.
  function recordOnCaseControl(d, subject, post, api, ctx) {
    const form = d.el("form", "ow-follow-up ow-record-case");
    const caseInput = d.el("input");
    caseInput.setAttribute("aria-label", "Case id (empty: a new case for this answer)");
    caseInput.setAttribute("placeholder", "case id, or empty for a new case");
    const send = d.el("button", "button subtle", "Record on case");
    send.type = "submit";
    const show = d.el("button", "ow-link-button", "Show case");
    show.type = "button";
    const said = d.el("small", "ow-review-status");
    const slot = d.el("div", "ow-case-records-slot");
    d.add(form, d.el("h3", "", "Record follow-ups and draft requests on a case"), caseInput, send, show, said, slot,
      d.el("small", "ow-muted", "Recorded on the Workbench case only: nothing is sent, proposed or approved."));
    const caseId = () => String(caseInput.value || "").trim();
    form.addEventListener("submit", async event => {
      if (event && event.preventDefault) event.preventDefault();
      said.textContent = "Recording…";
      try {
        const named = caseId();
        const data = await post(RECORD_ROUTE, {family: subject.family, root: subject.object_id, ...(named ? {case_id: named} : {})});
        const counts = {};
        list(data.recorded).forEach(item => { counts[item.outcome] = (counts[item.outcome] || 0) + 1; });
        caseInput.value = data.case_id || named;
        const pages = data.answer_pages || {};
        said.textContent = `On case ${data.case_id || "?"}: ${Object.keys(counts).map(k => `${counts[k]} ${words(k)}`).join(", ") || "nothing asks a person"}. Nothing was sent.`
          + (pages.complete === false ? ` Partial (${words(pages.partial || "not complete")}): ${pages.read} answer page(s) were read; a gap whose records were not read is not recorded.` : "");
        d.clear(slot).appendChild(caseRecordsNode(d, data.case, ctx));
      } catch (error) { said.textContent = "Not recorded: " + plainError(error); }
    });
    show.addEventListener("click", async () => {
      if (!caseId()) { said.textContent = "Name a case to show."; return; }
      try {
        const data = await api(`${CASE_ROUTE}?case_id=${encodeURIComponent(caseId())}`);
        d.clear(slot).appendChild(caseRecordsNode(d, data.case, ctx));
        said.textContent = "";
      } catch (error) { said.textContent = "Not shown: " + plainError(error); }
    });
    return form;
  }

  // A case's recorded follow-ups and draft requests as the case store shows them to you now:
  // each citation re-resolved for you; an item citing a record you cannot open now shows no
  // text, only that it is withheld.
  function caseRecordsNode(d, data, ctx) {
    const c = data || {};
    const box = d.el("div", "ow-case-records");
    box.setAttribute("data-case-id", c.case_id || "");
    d.add(box, d.el("h3", "", `Case ${c.case_id || "?"} (${words(c.status || "unknown")}): follow-ups and draft requests`));
    const cites = item => list(item.citations).filter(cite => cite.l1_handle).map(cite => {
      const node = d.el("span", "ow-case-cite");
      d.add(node, citationNode(d, {l1_handle: cite.l1_handle}, {...ctx, openable: Boolean(ctx && ctx.openable && cite.openable)}),
        cite.status !== "resolved" ? tagNode(d, cite.status, "warn") : null);
      return node;
    });
    const withheld = item => d.el("span", "ow-withheld", `Withheld: it cites a record you cannot open now (${words(item.content || "withheld")}).`);
    const ups = d.el("ul", "ow-claims ow-case-follow-ups");
    list(c.follow_ups).forEach(item => {
      const li = d.el("li", "ow-claim ow-case-follow-up");
      li.setAttribute("data-record-id", item.record_id || "");
      if (item.missing === null || item.missing === undefined) li.appendChild(withheld(item));
      else d.add(li, d.el("span", "ow-claim-text", item.missing), tagNode(d, item.state || "unknown", STATE_CLASS[item.state] || "warn"),
        d.el("span", "ow-reason", "who can answer: " + list(item.who_can_answer).join(", ")),
        item.why ? d.el("small", "ow-muted", item.why) : null, ...cites(item));
      ups.appendChild(li);
    });
    const drafts = d.el("ul", "ow-claims ow-case-drafts");
    list(c.drafts).forEach(item => {
      const li = d.el("li", "ow-claim ow-case-draft");
      li.setAttribute("data-record-id", item.record_id || "");
      if (item.statement === null || item.statement === undefined) li.appendChild(withheld(item));
      else d.add(li, tagNode(d, item.superseded ? "draft, superseded" : "draft, not sent", "info"),
        d.el("span", "ow-claim-text", item.statement), ...cites(item));
      drafts.appendChild(li);
    });
    d.add(box, d.el("h3", "", `Follow-ups (${list(c.follow_ups).length})`),
      list(c.follow_ups).length ? ups : d.el("p", "ow-muted", "No follow-up is recorded on this case."),
      d.el("h3", "", `Draft requests (${list(c.drafts).length})`),
      list(c.drafts).length ? drafts : d.el("p", "ow-muted", "No draft request is recorded on this case."),
      c.truncated ? d.el("p", "ow-partial", "More are recorded than are listed here.") : null);
    return box;
  }

  // ---- one object ----------------------------------------------------------------

  function banner(d, data) {
    const ev = data.evidence || {};
    const n = d.el("div", "ow-banner" + (ev.fixture ? " ow-fixture" : ""));
    d.add(n, d.el("strong", "", ev.fixture ? "FIXTURE · " + (ev.label || "") : (ev.label || "evidence")),
      d.el("span", "", ev.note || ""), d.el("span", "ow-readonly", data.note || "Read-only."));
    const store = data.store || {};
    // The employee reads when the data was read and whether it is current; the store's
    // build and mirror generation are an engineer's detail.
    const asOf = d.el("span", "ow-store ow-store-" + (store.state || "unknown"));
    d.add(asOf, d.el("span", "", "Data as of "), store.built_at ? timeNode(d, store.built_at) : d.el("span", "", "an unknown time"),
      d.el("span", "", store.state === "current" ? " · current" : ` · ${words(store.state || "unknown")}${store.detail ? ": " + store.detail : ""}`));
    const built = `Projection built ${store.built_at || "at an unknown time"} from mirror generation ${store.mirror_generation || "unknown"}`;
    d.add(n, asOf, engineerNode(d, "Engineer detail", [store.state === "current" ? built + " · current"
      : `${built} · ${words(store.state || "unknown")}${store.detail ? ": " + store.detail : ""}`]));
    // Per source: a capture the store did not project, or one it projected that is gone.
    const sources = store.sources || {};
    [["not_projected", "not projected"], ["removed", "removed since projection"]].forEach(([state, said]) => {
      const labels = Object.keys(sources).filter(source => (sources[source] || {}).state === state)
        .map(source => sources[source].label || source);
      if (labels.length) d.add(n, d.el("span", "ow-store-" + state.replace(/_/g, "-"),
        labels.map(label => `${label} ${said}`).join(" · ") + "."));
    });
    const principals = list(data.actor && data.actor.principals);
    if (principals.length) d.add(n, d.el("span", "ow-reader", `Reading as ${principals.join(", ")}: only what this identity may read is shown.`));
    const withheld = list(data.withheld_sources);
    if (withheld.length) d.add(n, d.el("span", "ow-withheld-sources",
      "Withheld: " + withheld.map(item => `${item.label || item.source} (${item.reason})`).join(", ") + "."));
    return n;
  }

  function cardSection(d, card, ctx) {
    const object = card.object || {};
    const s = section(d, displayOf(object), `${typeLabel(object.object_type)} · ${lineageWords(object.lineage)}`, "card");
    const idNode = d.el("code", "ow-object-id", labelOf(object.object_id));
    idNode.setAttribute("title", object.object_id || "");
    d.add(s, idNode);
    const values = d.el("div", "ow-values");
    list(card.values).forEach(value => {
      const row = d.el("div", "ow-value-row");
      row.setAttribute("data-property", value.property);
      d.add(row, d.el("span", "ow-prop", words(value.property)), valueNode(d, value),
        citationNode(d, value.citation, ctx),
        value.authority_rank !== null && value.authority_rank !== undefined ? d.el("span", "ow-rank", `authority #${value.authority_rank + 1}`) : null,
        markingsNode(d, value.markings), valueReviewNode(d, value),
        extensionSlot(d, ctx, "value_review", {kind: "value", object_id: object.object_id, property: value.property,
          value_type: value.value_type, citation: value.citation, review: value.review,
          // An empty source field has nothing to confirm (only a correction can say what it reads).
          empty: valueText(value.value) === null}));
      values.appendChild(row);
    });
    if (!list(card.values).length) values.appendChild(d.el("p", "ow-muted", "No value of this object is visible to you."));
    d.add(s, values, withheldNode(d, card.withheld), completenessNode(d, card.completeness));
    if (identityCandidates(ctx.explanation).length) {
      const ids = d.el("div", "ow-identity");
      d.add(ids, d.el("h3", "", "Identity (candidates, never merged)"));
      identityCandidates(ctx.explanation).forEach(edge => ids.appendChild(edgeNode(d, edge, card.object && card.object.object_id, {}, ctx)));
      s.appendChild(ids);
    }
    return s;
  }

  // One row per identity candidate, exactly as the server sent them
  // (explanation.identity: a link stored in both directions is already one candidate with
  // its features merged and its differing tiers said). Nothing is merged here.
  function identityCandidates(explanation) {
    return list(explanation && explanation.identity);
  }

  // Who read a value and a person's review of it in force, beside the source value (never
  // in its place): a confirmation, or a correction with the value the person read and
  // where (page or span of the cited record).
  function valueReviewNode(d, value) {
    const review = value.review;
    if (!value.read_by && !review) return null;
    const box = d.el("span", "ow-value-review");
    if (value.read_by) box.appendChild(tagNode(d, READ_BY[value.read_by] || value.read_by, value.read_by === "human_reviewed" ? "ok" : "warn"));
    if (!review) return box;
    if (value.tier) box.appendChild(tagNode(d, "source value: " + value.tier, TIER_CLASS[value.tier]));
    const amount = [valueText(review.value), review.unit, review.currency].filter(Boolean).join(" ");
    const said = d.el("span", review.decision === "correct_value" ? "ow-corrected" : "ow-confirmed",
      `${review.reviewed_by} ${review.decision === "correct_value" ? "corrected it to " + amount : "confirmed it"} · ${review.tier} · `);
    d.add(box, said, timeNode(d, review.reviewed_at));
    list(review.evidence).forEach(e => {
      const cites = d.el("small", "ow-muted",
        `cites the source record${e.page ? " page " + e.page : ""}${e.start !== undefined ? ` characters ${e.start}–${e.end}` : ""}`);
      cites.setAttribute("title", `${e.l1_handle}${e.field_path ? " " + e.field_path : ""}`);
      box.appendChild(cites);
    });
    return box;
  }

  // Confirm or correct one value (POST /api/object-review). A confirmation cites the
  // value's own citation; a correction states the value as read (with its unit or
  // currency) and the page of the cited original it was read on. The server re-checks
  // every cited version and refuses a correction without a page or span.
  function valueReviewControl(d, subject, post) {
    const c = subject.citation;
    if (!c || !c.l1_handle || !c.digest || !CORRECTABLE.has(subject.value_type)) return null;
    const box = d.el("div", "ow-value-control");
    const said = d.el("small", "ow-review-status");
    const buttons = [];
    // While a review is sent its buttons are disabled; after it (recorded or refused) each
    // has its own label back.
    const send = async body => {
      said.textContent = "Recording…";
      buttons.forEach(([button]) => { button.disabled = true; });
      try {
        const data = await post(REVIEW_ROUTE, body);
        const review = (data.review || {}).review || {};
        said.textContent = `Recorded (${words(review.state || "recorded")}); it shows beside the source value from the next read. Nothing was written to any business system.`;
      } catch (error) { said.textContent = "Not recorded: " + plainError(error); }
      finally { buttons.forEach(([button, label, enabled]) => { button.textContent = label; button.disabled = !enabled(); }); }
    };
    if (!subject.empty) {
      const confirm = d.el("button", "button subtle", "Confirm value");
      confirm.type = "button";
      confirm.setAttribute("data-decision", "confirm_value");
      confirm.addEventListener("click", () => send({decision: "confirm_value", subject: subject.object_id,
        value: {property: subject.property}, evidence: [{l1_handle: c.l1_handle, digest: c.digest, field_path: c.field_path}]}));
      box.appendChild(confirm);
      buttons.push([confirm, "Confirm value", () => true]);
    }
    const form = d.el("form", "ow-correct");
    // Each input sits in its own visible label (the label names it; no placeholder-only field).
    const field = (label, placeholder) => {
      const input = d.el("input");
      input.setAttribute("placeholder", placeholder);
      input.setAttribute("aria-label", label);
      const wrap = d.el("label", "ow-correct-field");
      d.add(wrap, d.el("span", "ow-correct-label", label), input);
      d.add(form, wrap);
      return input;
    };
    const value = field("Correct value", subject.value_type === "date" ? "YYYY-MM-DD" : "the value as you read it");
    const unit = ["quantity", "money", "decimal"].includes(subject.value_type) ? field("Unit", subject.value_type === "quantity" ? "unit, e.g. ea" : "per unit (optional)") : null;
    const currency = subject.value_type === "money" ? field("Currency", "e.g. USD") : null;
    const page = field("Page of the original you read it on", "page");
    const correct = d.el("button", "button subtle", "Correct value");
    correct.type = "submit";
    correct.setAttribute("data-decision", "correct_value");
    // A correction states the value read: the button waits until there is one.
    const filled = () => Boolean(String(value.value || "").trim());
    correct.disabled = true;
    value.addEventListener("input", () => { correct.disabled = !filled(); });
    buttons.push([correct, "Correct value", filled]);
    form.appendChild(correct);
    form.addEventListener("submit", event => {
      event.preventDefault();
      if (!filled()) { said.textContent = "Enter the value as you read it before correcting."; return undefined; }
      const reading = {property: subject.property, value: String(value.value).trim()};
      if (unit && unit.value.trim()) reading.unit = unit.value.trim();
      if (currency && currency.value.trim()) reading.currency = currency.value.trim().toUpperCase();
      const cited = {l1_handle: c.l1_handle, digest: c.digest};
      const number = Number.parseInt(page.value, 10);
      if (Number.isInteger(number) && number > 0) cited.page = number;
      return send({decision: "correct_value", subject: subject.object_id, value: reading, evidence: [cited]});
    });
    box.appendChild(form);
    box.appendChild(said);
    return box;
  }

  // The evidence features of a link: each with its verdict and, when the tool cited
  // the values it compared, those citations (why the link exists, or why not).
  // A feature the traverse tool adds with no verdict reads as its meaning, not "?".
  const FEATURE_WORDS = {never_authority_lineage: "one side rests on a copy that is never the authority"};
  function features(d, items, cls, ctx) {
    const box = d.el("span", "ow-features");
    list(items).forEach(f => {
      const said = FEATURE_WORDS[f.feature];
      const item = d.el("span", "ow-feature " + cls, said && f.agrees !== true && f.agrees !== false ? said
        : `${said || words(f.feature)}${f.agrees === true ? " ✓" : f.agrees === false ? " ✗" : " ?"}`);
      if (said) item.setAttribute("title", f.feature);
      list(f.citations).forEach(c => item.appendChild(citationNode(d, c, ctx)));
      box.appendChild(item);
    });
    return box;
  }

  function whyNode(d, edge, ctx) {
    const why = d.el("div", "ow-why");
    d.add(why, d.el("span", "ow-why-label", "Why linked:"), features(d, edge.reason, "ow-for", ctx));
    if (list(edge.why_not).length) d.add(why, d.el("span", "ow-why-label", "Against:"), features(d, edge.why_not, "ow-against", ctx));
    const note = TIER_NOTE[edge.tier];
    if (note) why.appendChild(d.el("small", "ow-muted", note));
    return why;
  }

  function edgeNode(d, edge, rootId, objects, ctx) {
    const otherId = edge.from === rootId ? edge.to : edge.from;
    const other = objects[otherId] || {object_id: otherId};
    const row = d.el("div", "ow-edge ow-tier-" + (TIER_CLASS[edge.tier] || "info"));
    const open = d.el("button", "ow-link-button", displayOf(other));
    open.type = "button";
    open.setAttribute("data-object-id", otherId || "");
    open.setAttribute("title", otherId || "");
    open.addEventListener("click", () => ctx.onOpen && ctx.onOpen(otherId));
    d.add(row, tagNode(d, edge.tier === "contradicted" ? "contradicted" : edge.tier,
      TIER_CLASS[edge.tier]), open, other.object_type ? tagNode(d, typeLabel(other.object_type)) : null,
      other.lineage ? d.el("span", "ow-system", "from " + lineageWords(other.lineage)) : null,
      whyNode(d, edge, ctx), markingsNode(d, edge.markings),
      edge.tiers_differ ? d.el("p", "ow-partial", `Recorded in both directions with different tiers (${edge.tiers_differ.map(words).join(", ")}): the lower one holds until a person reviews it.`) : null,
      extensionSlot(d, ctx, "review", {kind: "link", link_id: edge.link_id, link_type: edge.link_type,
        from: edge.from, to: edge.to, tier: edge.tier, evidence: reviewEvidence(edge, ctx.rootCitation),
        reviews: list(ctx.reviews).filter(r => r.link_type === edge.link_type && list(edge.directions || [edge])
          .some(e => r.subject === e.from && r.target === e.to))}));
    return row;
  }

  // What a person reviewing a link was shown: the cited record versions of its reason
  // and why-not features (handle, digest, field), else the version of the open object's
  // record its card cites. The server re-checks each against the current version.
  function reviewEvidence(edge, fallback) {
    const out = [], seen = new Set();
    [...list(edge.reason), ...list(edge.why_not)].forEach(feature => list(feature.citations).forEach(c => {
      if (!c || !c.l1_handle || !c.digest || seen.has(c.l1_handle) || out.length >= MAX_REVIEW_EVIDENCE) return;
      seen.add(c.l1_handle);
      out.push(c.field_path ? {l1_handle: c.l1_handle, digest: c.digest, field_path: c.field_path} : {l1_handle: c.l1_handle, digest: c.digest});
    }));
    if (!out.length && fallback && fallback.l1_handle && fallback.digest)
      out.push({l1_handle: fallback.l1_handle, digest: fallback.digest, field_path: fallback.field_path});
    return out;
  }

  const DECISION_LABEL = {accept_link: "confirmed", reject_link: "rejected"};

  // plx.aggregate's word on the open object as the supply of a quantity over one
  // many-to-many link (operator_ui/object_workspace.py ALLOCATION_STATES).
  const ALLOCATION_NOTE = {
    allocation_review_required: ["warn", "it reaches several records: a person allocates each its share; it is never split by inference"],
    allocation_conflict: ["hot", "the allocations recorded do not fit its own value (more than it, or another unit): it is not split"],
    group_link_withheld: ["info", "a link of it is withheld from you: how it splits is unknown to you"],
    allocation_remainder_unallocated: ["warn", "the allocations give out less than its value; the rest is in no record"],
    reviewed_allocation_used: ["ok", "split by a person's allocations"],
    n_to_m_single_reach: ["info", "it reaches one record: counted whole there, by reach, not by an allocation"],
    not_split: ["", "not split"], error: ["hot", "the allocation state could not be read"]};

  function allocationsSection(d, data, ctx) {
    const allocations = data.allocations || {};
    if (allocations.status !== "read" || !list(allocations.items).length) return null;
    const s = section(d, "Allocations", "how a quantity of this object is shared over a many-to-many link; never by inference", "allocations");
    list(allocations.items).forEach(item => {
      const box = d.el("div", "ow-allocation");
      box.setAttribute("data-state", item.state);
      const [cls, note] = ALLOCATION_NOTE[item.state] || ["warn", words(item.state)];
      d.add(box, d.el("h3", "", `${words(item.property)} over ${item.link_type}`), tagNode(d, item.state, cls), d.el("span", "ow-reason", note));
      list(item.values).forEach(v => {
        const row = d.el("div", "ow-value-row");
        d.add(row, d.el("span", "ow-prop", "its own value"), d.el("span", "ow-value", `${valueText(v.value) || "—"} ${v.unit || "(no unit)"}`), citationNode(d, v.citation, ctx));
        box.appendChild(row);
      });
      if (list(item.reached).length) box.appendChild(d.el("p", "ow-muted", `Reaches ${list(item.reached).join(", ")}`));
      list(item.gaps).forEach(gap => box.appendChild(gapNode(d, gap)));
      if (item.error) box.appendChild(errorNode(d, item.error));
      if (item.state === "allocation_review_required")
        d.add(box, extensionSlot(d, ctx, "allocate", {kind: "allocation", object_id: data.object_id, link_type: item.link_type,
          property: item.property, values: item.values, reached: item.reached}));
      s.appendChild(box);
    });
    if (allocations.truncated) s.appendChild(d.el("p", "ow-partial", "Only the first quantities and links were checked."));
    return s;
  }

  // Allocate a share of the object's own quantity to one record it reaches (POST
  // /api/object-review, a plx.review allocate decision citing that value). The share is in
  // the value's own unit; plx.review refuses one that does not fit it (allocation_conflict).
  function allocateControl(d, subject, post) {
    const box = d.el("div", "ow-allocate");
    // A value empty in its source is no candidate (as plx.aggregate picks it).
    const present = list(subject.values).filter(v => v.value !== null && v.value !== undefined);
    const shown = new Set(present.map(v => `${v.value}|${v.unit}`));
    const own = present[0];
    const c = own && own.citation;
    if (shown.size !== 1 || !own.unit || !c || !c.l1_handle || !c.digest) {
      box.appendChild(d.el("small", "ow-muted", "No single cited value with a unit to allocate from."));
      return box;
    }
    list(subject.reached).forEach(target => {
      const form = d.el("form", "ow-allocate-row");
      form.setAttribute("data-target", target);
      const quantity = d.el("input");
      quantity.setAttribute("placeholder", `share of ${own.value} ${own.unit}`);
      quantity.setAttribute("aria-label", `Share for ${labelOf(target)} in ${own.unit}`);
      const go = d.el("button", "button subtle", "Allocate");
      go.type = "submit";
      const said = d.el("small", "ow-review-status");
      d.add(form, d.el("span", "ow-prop", labelOf(target)), quantity, d.el("span", "ow-unit", own.unit), go, said);
      form.addEventListener("submit", async event => {
        if (event && event.preventDefault) event.preventDefault();
        const share = String(quantity.value || "").trim();
        said.textContent = "Recording…";
        try {
          const data = await post(REVIEW_ROUTE, {decision: "allocate", subject: subject.object_id, target,
            link_type: subject.link_type, allocation: {property: subject.property, quantity: share, unit: own.unit},
            evidence: [{l1_handle: c.l1_handle, digest: c.digest, field_path: c.field_path}]});
          const review = (data.review || {}).review || {};
          said.textContent = `Allocated ${share} ${own.unit} of ${own.value} ${own.unit} (${words(review.state || "recorded")}); it applies at the next projection build. Nothing was written to any business system.`;
        } catch (error) { said.textContent = "Not recorded: " + plainError(error); }
      });
      box.appendChild(form);
    });
    return box;
  }

  // Confirm / reject one link (POST /api/object-review). The answer is what the review
  // store recorded (or the tool's refusal); the link's tier changes at the next build.
  function reviewControl(d, subject, post) {
    const box = d.el("div", "ow-review");
    list(subject.reviews).forEach(r => {
      const line = d.el("p", "ow-reviewed",
        `${r.author && r.author.principal ? r.author.principal : "a person"} ${DECISION_LABEL[r.decision] || words(r.decision)} this link · ${words(r.state)}${r.recorded_at ? " · " : ""}`);
      if (r.recorded_at) line.appendChild(timeNode(d, r.recorded_at));
      box.appendChild(line);
    });
    if (!list(subject.evidence).length) { box.appendChild(d.el("small", "ow-muted", "No cited record version to review against.")); return box; }
    const said = d.el("small", "ow-review-status");
    const decide = decision => async () => {
      said.textContent = "Recording…";
      try {
        const data = await post(REVIEW_ROUTE, {decision, subject: subject.from, target: subject.to,
          link_type: subject.link_type, evidence: subject.evidence});
        const review = (data.review || {}).review || {};
        said.textContent = `You ${DECISION_LABEL[decision]} this link (${words(review.state || "recorded")}); it applies at the next projection build. Nothing was written to any business system.`;
      } catch (error) { said.textContent = "Not recorded: " + plainError(error); }
    };
    [["accept_link", "Confirm link"], ["reject_link", "Reject link"]].forEach(([decision, label]) => {
      const button = d.el("button", "button subtle", label);
      button.type = "button";
      button.setAttribute("data-decision", decision);
      button.addEventListener("click", decide(decision));
      box.appendChild(button);
    });
    box.appendChild(said);
    return box;
  }

  // Propose a follow-up (POST /api/object-follow-up): what is missing, why, who can
  // answer. It becomes a Mission Control follow-up task proposal on the case chosen (an
  // open case citing this object: the one the reader came from, or the only one, is
  // preselected) or on a new case; its exact call waits for a person's approval (never
  // the proposer's). An empty field is named before anything is sent.
  // Why a recorded follow-up cannot ask for approval yet (ActionPath.prepare_call refusals,
  // sent as proposal.not_prepared), in words; an unknown code falls back to plainError.
  const NOT_PREPARED = {
    subject_unknown: "the record it is about is not known to the action path",
    not_an_mc_call: "it is not a Mission Control request",
    mc_operator_not_configured: "this console has no Mission Control operator set up",
    mc_follow_up_bucket_not_configured: "this console has no Mission Control follow-up bucket set up",
    mc_description_too_long: "the description is too long for Mission Control; shorten what is missing or why it matters",
    proposal_not_readable: "the proposal could not be read back",
    precondition_not_met: "a precondition of the request is not met",
    cited_source_unavailable: "a cited source cannot be read now"};
  function notPreparedWords(code) {
    return Object.prototype.hasOwnProperty.call(NOT_PREPARED, code) ? NOT_PREPARED[code] : plainError(code).replace(/\.$/, "");
  }

  function followUpControl(d, subject, post) {
    const form = d.el("form", "ow-follow-up");
    const field = (label, placeholder) => {
      const input = d.el("input");
      input.setAttribute("placeholder", placeholder);
      input.setAttribute("aria-label", label);
      d.add(form, d.el("label", "ow-follow-up-label", label), input);
      return input;
    };
    d.add(form, d.el("h3", "", "Propose a follow-up"));
    const missing = field("What is missing", "e.g. The supplier's confirmed ship date");
    const why = field("Why it matters", "e.g. The receipt is late and production waits on it");
    const who = field("Who can answer (comma separated)", "e.g. purchasing, the supplier");
    const open = list(subject.cases).filter(c => c && c.case_id && c.status === "open");
    let onCase = null;
    if (open.length || subject.case_id) {
      onCase = d.el("select");
      onCase.setAttribute("aria-label", "Case to propose it on");
      const options = [];
      const option = (value, label) => { const o = d.el("option", "", label); o.value = value; o.setAttribute("value", value); onCase.appendChild(o); options.push(o); return o; };
      option("", "A new case");
      // A long case question is shortened in the list; the whole question is the option's title.
      open.forEach(c => {
        const question = c.question ? String(valueText(c.question)) : "case";
        const shown = question.length > 72 ? "…" + question.slice(-71) : question;
        option(c.case_id, `${shown} (${c.case_id})`).setAttribute("title", question);
      });
      if (subject.case_id && !open.some(c => c.case_id === subject.case_id)) option(subject.case_id, `the case you came from (${subject.case_id})`);
      const chosen = subject.case_id || (open.length === 1 ? open[0].case_id : "");
      options.forEach(o => { o.selected = o.value === chosen; });
      onCase.value = chosen;
      d.add(form, d.el("label", "ow-follow-up-label", "On case"), onCase);
    }
    const send = d.el("button", "button subtle", "Propose");
    send.type = "submit";
    const said = d.el("small", "ow-review-status");
    d.add(form, send, said, d.el("small", "ow-muted", "A proposal only: a person approves it before anything happens."));
    form.addEventListener("submit", async event => {
      if (event && event.preventDefault) event.preventDefault();
      const body = {object_id: subject.object_id, missing: String(missing.value || "").trim(),
        why: String(why.value || "").trim(), who_can_answer: String(who.value || "").split(",").map(x => x.trim()).filter(Boolean)};
      const unsaid = !body.missing ? "follow_up_missing_required" : !body.why ? "follow_up_why_required"
        : !body.who_can_answer.length ? "follow_up_who_can_answer_required" : null;
      if (unsaid) { said.textContent = plainError(unsaid); return; }
      const caseId = onCase ? String(onCase.value || "").trim() : "";
      if (caseId) body.case_id = caseId;
      said.textContent = "Proposing…";
      try {
        const data = await post(FOLLOW_UP_ROUTE, body);
        const p = data.proposal || {};
        const stage = (p.approval && p.approval.label) || p.status || "awaits approval";
        said.textContent = p.not_prepared
          ? `Recorded on case ${p.case_id || "?"} as ${p.proposal_id || "?"}, but it cannot request approval yet: ${notPreparedWords(p.not_prepared)}. Nothing was executed.`
          : `Proposed as ${p.proposal_id || "?"} on case ${p.case_id || "?"}: ${stage}. Nothing was executed.`;
      } catch (error) { said.textContent = "Not proposed: " + plainError(error); }
    });
    return form;
  }

  // The agent loop's answer (operator_ui/object_workspace.py ObjectWorkspace.ask): each
  // kept claim with its citations and claim-check status (passed, or unverified with the
  // check's codes); unsupported claims under their own label; what is unknown. Model text
  // is set as text, never markup.
  function answerSection(d, data, ctx) {
    const a = data.answer || {};
    const s = section(d, "Answer", "the agent read only what your access returns; every claim is cited", "answer");
    const outcome = d.el("p", "ow-answer-outcome ow-" + (a.outcome === "answered" ? "ok" : "warn"),
      (OUTCOME_LABEL[a.outcome] || words(a.outcome)) + (a.reason ? ": " + words(a.reason) : ""));
    d.add(s, outcome);
    const claims = d.el("ul", "ow-claims");
    list(a.claims).forEach(claim => {
      const li = d.el("li", "ow-claim");
      const check = claim.check || {};
      const passed = check.status === "passed";
      d.add(li, d.el("span", "ow-claim-text", claim.text),
        d.el("span", "ow-tag ow-" + (passed ? "ok" : "warn"), passed ? "claim checks passed"
          : "not verified: " + list(check.failures).map(f => words(f.code)).join(", ")));
      list(check.failures).forEach(f => li.appendChild(d.el("small", "ow-muted", `${f.value}: ${f.detail || words(f.code)}`)));
      list(claim.citations).forEach(c => li.appendChild(citationNode(d, {l1_handle: c.l1_handle},
        {...ctx, openable: Boolean(ctx && ctx.openable && c.openable)})));
      claims.appendChild(li);
    });
    if (list(a.claims).length) s.appendChild(claims);
    if (list(a.unsupported).length) {
      const box = d.el("div", "ow-unsupported");
      box.appendChild(d.el("h3", "", "Unsupported: not part of the answer"));
      // The claim gate's failure codes say why; a claim citing a record withheld from
      // you shows no text.
      list(a.unsupported).forEach(u => {
        const why = [words(u.reason), ...list(u.failures).map(f => words(f.code))].join(": ");
        box.appendChild(d.el("p", "ow-claim ow-warn", u.text === null || u.text === undefined
          ? `Text withheld: it cites a record you may not open (${why})` : `${u.text} (${why})`));
        list(u.failures).forEach(f => {
          if (u.text !== null && u.text !== undefined && (f.value || f.detail)) {
            box.appendChild(d.el("small", "ow-muted", `${f.value}: ${f.detail || words(f.code)}`));
          }
        });
      });
      s.appendChild(box);
    }
    const hidden = (a.dropped_claims || 0) - list(a.unsupported).length;
    if (hidden > 0) s.appendChild(d.el("p", "ow-muted", `${hidden} more claim${hidden === 1 ? "" : "s"} dropped without text.`));
    if (list(a.unknowns).length) {
      const ul = d.el("ul", "ow-unknowns");
      list(a.unknowns).forEach(u => ul.appendChild(d.el("li", "", u)));
      d.add(s, d.el("h3", "", "Unknown"), ul);
    }
    if (data.note) s.appendChild(d.el("small", "ow-muted", data.note));
    return s;
  }

  // Ask about the object in view (POST /api/object-ask): the question and, when named, the
  // case to keep the session on; who asks is the session, never a body field.
  function askControl(d, subject, post, api, ctx) {
    const form = d.el("form", "ow-ask");
    const question = d.el("textarea");
    question.setAttribute("rows", "2");
    question.setAttribute("aria-label", "Ask about this object");
    question.setAttribute("placeholder", "e.g. Is it fully received, and what is still missing?");
    const caseInput = d.el("input");
    caseInput.setAttribute("aria-label", "Case id to keep this session on (optional)");
    caseInput.setAttribute("placeholder", "case id to keep the session on (optional)");
    const send = d.el("button", "button", "Ask");
    send.type = "submit";
    const show = d.el("button", "ow-link-button", "Sessions on this case");
    show.type = "button";
    const said = d.el("small", "ow-review-status");
    const slot = d.el("div", "ow-answer-slot");
    const sessions = d.el("div", "ow-sessions-slot");
    d.add(form, d.el("h3", "", "Ask about this object"), question, caseInput, send, show, said, slot, sessions);
    const caseId = () => String(caseInput.value || "").trim();
    form.addEventListener("submit", async event => {
      if (event && event.preventDefault) event.preventDefault();
      said.textContent = "Investigating…";
      try {
        const named = caseId();
        const data = await post(ASK_ROUTE, {object_id: subject.object_id, question: String(question.value || "").trim(), ...(named ? {case_id: named} : {})});
        d.clear(slot).appendChild(answerSection(d, data, ctx));
        const kept = data.session;
        said.textContent = !kept ? "" : kept.recorded ? `Kept on case ${kept.case_id} as session ${kept.session_id}.`
          : `Not kept on case ${kept.case_id}: ${words(kept.refused || "refused")}.`;
      } catch (error) { said.textContent = "Not answered: " + plainError(error); }
    });
    show.addEventListener("click", async () => {
      const named = caseId();
      if (!named) { said.textContent = "Name a case to list its sessions."; return; }
      try {
        const data = await api(`${CASE_ROUTE}?case_id=${encodeURIComponent(named)}`);
        d.clear(sessions).appendChild(sessionsNode(d, named, data.sessions, post, ctx));
        said.textContent = "";
      } catch (error) { said.textContent = "Not shown: " + plainError(error); }
    });
    return form;
  }

  // A case's kept Ask sessions as the case store shows them to you now. A session that
  // touched a record you cannot read now is withheld: no question, call or citation.
  function sessionsNode(d, caseId, sessions, post, ctx) {
    const box = d.el("div", "ow-sessions");
    box.setAttribute("data-case-id", caseId);
    d.add(box, d.el("h3", "", `Sessions kept on case ${caseId} (${list(sessions).length})`));
    const ul = d.el("ul", "ow-claims");
    list(sessions).forEach(item => {
      const li = d.el("li", "ow-claim ow-session");
      li.setAttribute("data-session-id", item.session_id || "");
      if (item.withheld) {
        d.add(li, d.el("span", "ow-withheld", `Session ${item.session_id}: withheld: it touched a record you cannot read now.`));
        ul.appendChild(li);
        return;
      }
      const replay = d.el("button", "button subtle", "Replay");
      replay.type = "button";
      const report = d.el("div", "ow-replay-slot");
      d.add(li, d.el("span", "ow-claim-text", item.question),
        tagNode(d, (OUTCOME_LABEL[item.outcome] || words(item.outcome)) + (item.reason ? ": " + words(item.reason) : ""), item.outcome === "answered" ? "ok" : "warn"),
        d.add(d.el("span", "ow-reason", `session ${item.session_id} · ${item.call_count} tool call${item.call_count === 1 ? "" : "s"} · recorded `), timeNode(d, item.at)),
        ...list(item.citations).map(cite => {
          const node = d.el("span", "ow-case-cite");
          d.add(node, citationNode(d, {l1_handle: cite.handle}, {...ctx, openable: Boolean(ctx && ctx.openable && cite.openable)}),
            cite.status !== "unchanged" ? tagNode(d, cite.status, "warn") : null);
          return node;
        }), replay, report);
      replay.addEventListener("click", async () => {
        report.textContent = "Replaying…";
        try {
          const data = await post(REPLAY_ROUTE, {case_id: caseId, session_id: item.session_id});
          report.textContent = "";
          d.clear(report).appendChild(replayNode(d, data, ctx));
        } catch (error) { report.textContent = "Not replayed: " + plainError(error); }
      });
      ul.appendChild(li);
    });
    if (list(sessions).length) box.appendChild(ul);
    else box.appendChild(d.el("p", "ow-muted", "No session is kept on this case."));
    return box;
  }

  // A replay's divergence report: each call whose evidence or error state changed, each
  // cited record whose content changed; identical only when nothing diverged.
  function replayNode(d, data, ctx) {
    const r = (data && data.replay) || {};
    const box = d.el("div", "ow-replay");
    if (!r.replayed) {
      box.appendChild(d.el("p", "ow-withheld", "Not replayed: the session touched a record you cannot read now. No call was run."));
      return box;
    }
    box.appendChild(d.el("p", "ow-" + (r.identical ? "ok" : "warn"), r.identical
      ? `Identical: ${r.calls} call${r.calls === 1 ? "" : "s"} returned the same evidence and every cited record is unchanged.`
      : `Diverged: ${list(r.divergences).length} difference${list(r.divergences).length === 1 ? "" : "s"} from the record.`));
    const ul = d.el("ul", "ow-divergences");
    list(r.divergences).forEach(item => {
      const li = d.el("li", "ow-divergence ow-warn");
      li.setAttribute("data-kind", item.kind || "");
      const what = DIVERGENCE_LABEL[item.kind] || words(item.kind);
      if (item.handle) d.add(li, d.el("span", "", `Cited record: ${what} `),
        citationNode(d, {l1_handle: item.handle}, {...ctx, openable: Boolean(ctx && ctx.openable && item.openable)}));
      else li.appendChild(d.el("span", "", `Call ${(item.seq || 0) + 1} (${item.tool}, step ${item.step}) ${what}.`));
      ul.appendChild(li);
    });
    if (list(r.divergences).length) box.appendChild(ul);
    if (data.note) box.appendChild(d.el("small", "ow-muted", data.note));
    return box;
  }

  function renderAnswer(doc, data, ctx) { useLabels(data); return answerSection(dom(doc), data, ctx); }

  // A map label, never cut: the whole label wrapped by words onto lines of at most
  // MAP_LINE characters (a longer word on lines of its own).
  const MAP_LINE = 18;
  function mapLines(text) {
    const lines = [];
    String(text).split(/\s+/).filter(Boolean).forEach(word => {
      while (word.length > MAP_LINE) { lines.push(word.slice(0, MAP_LINE)); word = word.slice(MAP_LINE); }
      const last = lines.length ? lines[lines.length - 1] : null;
      if (last !== null && last.length + 1 + word.length <= MAP_LINE) lines[lines.length - 1] = last + " " + word;
      else if (word) lines.push(word);
    });
    return lines.length ? lines : [""];
  }
  function mapLabel(make, text, x, y) {
    const label = make("text");
    label.setAttribute("x", x); label.setAttribute("y", y); label.setAttribute("text-anchor", "middle");
    mapLines(text).forEach((line, i) => {
      const span = make("tspan");
      span.setAttribute("x", x);
      span.setAttribute("dy", i ? "1.1em" : "0");
      span.textContent = line;
      label.appendChild(span);
    });
    return label;
  }

  // A compact radial map (scales to any width through its viewBox); the lists below
  // carry the same edges with their evidence features.
  function mapSvg(d, doc, rootObject, nodes, ctx) {
    const ns = "http://www.w3.org/2000/svg";
    const make = tag => (doc.createElementNS ? doc.createElementNS(ns, tag) : doc.createElement(tag));
    const svg = make("svg");
    svg.setAttribute("viewBox", "0 0 360 260");
    svg.setAttribute("class", "ow-map");
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", `Relationship map: ${nodes.length} linked objects`);
    const cx = 180, cy = 130, r = 100;
    nodes.slice(0, MAP_NODES).forEach((node, i) => {
      const angle = (2 * Math.PI * i) / Math.min(nodes.length, MAP_NODES);
      const x = Math.round(cx + r * Math.cos(angle)), y = Math.round(cy + 0.9 * r * Math.sin(angle));
      const line = make("line");
      line.setAttribute("x1", cx); line.setAttribute("y1", cy); line.setAttribute("x2", x); line.setAttribute("y2", y);
      line.setAttribute("class", "ow-map-edge ow-tier-" + (TIER_CLASS[node.tier] || "info"));
      svg.appendChild(line);
      const group = make("g");
      group.setAttribute("class", "ow-map-node");
      group.setAttribute("tabindex", "0");
      const dot = make("circle");
      dot.setAttribute("cx", x); dot.setAttribute("cy", y); dot.setAttribute("r", "9");
      dot.setAttribute("class", "ow-tier-" + (TIER_CLASS[node.tier] || "info"));
      const label = mapLabel(make, displayOf(node.object), x, y + 21);
      const title = make("title");
      title.textContent = displayOf(node.object);
      group.appendChild(title); group.appendChild(dot); group.appendChild(label);
      group.addEventListener("click", () => ctx.onOpen && ctx.onOpen(node.object.object_id));
      group.addEventListener("keydown", e => { if (e && e.key === "Enter" && ctx.onOpen) ctx.onOpen(node.object.object_id); });
      svg.appendChild(group);
    });
    const center = make("circle");
    center.setAttribute("cx", cx); center.setAttribute("cy", cy); center.setAttribute("r", "14");
    center.setAttribute("class", "ow-map-root");
    const label = mapLabel(make, displayOf(rootObject), cx, cy + 30);
    svg.appendChild(center); svg.appendChild(label);
    return svg;
  }

  function aroundSection(d, doc, data, ctx) {
    const around = data.around || {hops: []};
    const rootObject = (data.card && data.card.object) || {object_id: data.object_id};
    const s = section(d, "Relationship map", `${list(around.hops).length} link types · weakest shown: ${words(weakestShown(around.hops))}`, "around");
    const nodes = [];
    const hops = d.el("div", "ow-hops");
    list(around.hops).forEach(hop => {
      const result = hop.result || {};
      const box = d.el("div", "ow-hop");
      const hopHead = d.el("h3", "", linkWords(`${hop.link_type} ${hop.direction}`));
      hopHead.setAttribute("title", `${hop.link_type} · ${hop.direction}`);
      d.add(box, hopHead);
      if (result.error) { box.appendChild(errorNode(d, result.error)); hops.appendChild(box); return; }
      const objects = {};
      list(result.objects).forEach(o => { objects[o.object_id] = o; });
      list(result.edges).forEach(edge => {
        box.appendChild(edgeNode(d, edge, data.object_id, objects, ctx));
        const otherId = edge.from === data.object_id ? edge.to : edge.from;
        nodes.push({tier: edge.tier, object: objects[otherId] || {object_id: otherId}});
      });
      if (!list(result.edges).length) box.appendChild(d.el("p", "ow-muted", "No link of this type is visible to you at this tier."));
      list(result.gaps).forEach(gap => box.appendChild(gapNode(d, gap)));
      d.add(box, withheldNode(d, result.withheld), completenessNode(d, result.completeness));
      hops.appendChild(box);
    });
    if (nodes.length) {
      const systems = {};
      nodes.forEach(n => { const key = lineageWords(n.object.lineage) || "unknown source"; systems[key] = (systems[key] || 0) + 1; });
      s.appendChild(d.el("p", "ow-systems", "Linked across sources: " + Object.keys(systems).sort().map(k => `${k} (${systems[k]})`).join(", ")));
      s.appendChild(mapSvg(d, doc, rootObject, nodes, ctx));
    }
    if (!list(around.hops).length) s.appendChild(d.el("p", "ow-muted", "This object lists no link visible to you."));
    if (around.truncated) s.appendChild(d.el("p", "ow-partial", `Showing ${around.hops.length} of ${around.link_types_listed} link types.`));
    s.appendChild(hops);
    return s;
  }

  // Quantities and money by role: every quantity or money value the card and the
  // process comparisons answered, grouped by role (or currency). Never summed or
  // converted: planned, produced and inspected stay distinct, and mixed units show apart.
  function amountsSection(d, data, ctx) {
    const s = section(d, "Quantities and money by role", "each value cited; never summed across roles, units or currencies", "amounts");
    const groups = {};
    const seen = new Set();
    function take(value, from) {
      if (!value || !AMOUNT_TYPES.has(value.value_type)) return;
      const c = value.citation || {};
      const key = `${value.property}|${c.l1_handle}|${c.field_path}`;
      if (seen.has(key)) return;
      seen.add(key);
      // An empty value has no currency or unit to be unknown: it is listed as empty, apart
      // from the values the source does hold.
      const empty = value.value === null || value.value === undefined;
      const group = value.quantity_role ? words(value.quantity_role)
        : value.value_type === "money" ? (empty ? "money: empty in this source" : "money" + (value.currency ? " " + value.currency : " (currency unknown)"))
        : "quantity (role not stated)";
      (groups[group] = groups[group] || []).push({value, from});
    }
    list((data.card || {}).values).forEach(v => take(v, "this object"));
    const process = data.process || {};
    if (process.status === "read" && process.result) list(process.result.quantity_checks).forEach(check =>
      list(check.values).forEach(v => take(v, "process: " + list(check.roles).map(words).join(" vs "))));
    Object.keys(groups).sort().forEach(name => {
      const box = d.el("div", "ow-amount-group");
      box.setAttribute("data-role", name);
      // Spellings of one unit (EA, each, pcs) are one unit after the Portal fold; the
      // warning names the units as the sources wrote them.
      const folded = new Set(groups[name].map(item => unitToken(item.value.unit) || item.value.currency || ""));
      const units = new Set(groups[name].map(item => item.value.unit || item.value.currency || ""));
      d.add(box, d.el("h3", "", name), folded.size > 1 ? d.el("p", "ow-partial", `Mixed units (${[...units].map(u => u || "none").join(", ")}): not comparable as stated.`) : null);
      groups[name].forEach(({value, from}) => {
        const row = d.el("div", "ow-value-row");
        d.add(row, d.el("span", "ow-prop", `${words(value.property)} · ${from}`), valueNode(d, value), citationNode(d, value.citation, ctx), markingsNode(d, value.markings));
        box.appendChild(row);
      });
      s.appendChild(box);
    });
    const held = Object.values(groups).flat().some(({value}) => value.value !== null && value.value !== undefined);
    if (!Object.keys(groups).length) s.appendChild(d.el("p", "ow-muted", "No quantity or money value of this object is visible to you."));
    else if (!held) s.appendChild(d.el("p", "ow-muted", "This record lists these amounts but holds no value for them."));
    // The same record in another system (an identity candidate) may hold the amounts this
    // one leaves empty: name it, never copy its values here.
    const twins = identityCandidates(data.explanation).map(edge => edge.from === data.object_id ? edge.to : edge.from);
    if (Object.keys(groups).length && !held && twins.length)
      s.appendChild(d.el("p", "ow-muted", `Its identity candidate${twins.length === 1 ? "" : "s"} ${twins.map(labelOf).join(", ")} may hold them: open ${twins.length === 1 ? "it" : "one"} from the card.`));
    d.add(s, withheldNode(d, (data.card || {}).withheld));
    return s;
  }

  function timelineSection(d, data, ctx) {
    const timeline = data.timeline || {events: []};
    const s = section(d, "Timeline", `${list(timeline.events).length} events${timeline.truncated ? " · partial" : ""}`, "timeline");
    const ol = d.el("ol", "ow-timeline");
    list(timeline.events).forEach(event => {
      const li = d.el("li", "ow-event");
      const when = d.el("span", "ow-when");
      if (event.time) when.appendChild(timeNode(d, event.time));
      else if (event.window) d.add(when, timeNode(d, event.window.start), d.el("span", "", " – "), timeNode(d, event.window.end));
      else when.appendChild(d.el("span", "ow-muted", "time unknown"));
      const eventName = d.el("strong", "", String(event.event_type || "").replace(/^([A-Z][A-Za-z0-9]*)\.([a-z0-9_]+)$/, (all, t, e) => `${typeWords(t)} ${words(e)}`));
      eventName.setAttribute("title", event.event_type || "");
      d.add(li, when, eventName,
        tagNode(d, event.time_basis),
        d.el("span", "ow-event-objects", list(event.objects).map(o => `${words(o.qualifier)}: ${labelOf(o.object_id)}`).join(" · ")),
        citationNode(d, event.citation, ctx), markingsNode(d, event.markings),
        list(event.found_from).length && event.found_from[0] !== "object" ? d.el("small", "ow-muted", "via " + event.found_from.map(linkWords).join(", ")) : null);
      ol.appendChild(li);
    });
    if (!list(timeline.events).length) s.appendChild(d.el("p", "ow-muted", "No event visible to you relates to this object or its neighbours."));
    s.appendChild(ol);
    list(timeline.calls).forEach(call => {
      const result = call.result || {};
      const w = withheldNode(d, result.withheld);
      if (w) { w.textContent = `Events reached over "${linkWords(call.root)}": ${w.textContent}`; w.setAttribute("title", call.root || ""); s.appendChild(w); }
      if (result.error) s.appendChild(errorNode(d, result.error));
    });
    if (timeline.truncated) s.appendChild(d.el("p", "ow-partial", `Partial: showing ${list(timeline.events).length} of ${timeline.total} events, or a call was cut at its page cap.`));
    return s;
  }

  function stageNode(d, stage, name, ctx) {
    const row = d.el("div", "ow-stage ow-state-" + (STATE_CLASS[stage.state] || "info"));
    const undecided = stage.missing && stage.missing.state === "unknown";
    const label = stage.state === "missing" ? (undecided ? "undecided" : "not reached") : words(stage.state);
    d.add(row, d.el("strong", "", words(name)), tagNode(d, label, undecided ? "warn" : STATE_CLASS[stage.state]));
    if (stage.missing) {
      const detail = stage.missing.detail;
      const plain = detail && !ENGINEER_TEXT.test(detail);
      d.add(row, d.el("span", "ow-reason", `${words(stage.missing.reason)}${plain ? ": " + plainText(detail) : ""}`),
        detail && !plain ? engineerNode(d, "Engineer detail", [detail]) : null);
    }
    list(stage.citations).forEach(c => row.appendChild(citationNode(d, c, ctx)));
    return row;
  }

  function processSection(d, data, ctx) {
    const process = data.process || {status: "not_configured"};
    const s = section(d, "Process", process.status === "read" ? words(process.result.process) : words(process.status), "process");
    if (process.status === "not_applicable") { s.appendChild(d.el("p", "ow-muted", `No process applies to this object type (${process.hint || process.detail || ""}).`)); return s; }
    if (process.status === "not_configured") { s.appendChild(d.el("p", "ow-muted", "plx.process is not served by this console.")); return s; }
    if (process.status === "error") { s.appendChild(errorNode(d, process.error || {})); return s; }
    const result = process.result;
    if (result.error) { s.appendChild(errorNode(d, result.error)); return s; }
    if ("current_stage" in result) s.appendChild(d.el("p", "ow-current", result.current_stage === null
      ? "Every stage is reached." : `Current stage: ${words(result.current_stage)}`));
    const stages = d.el("div", "ow-stages");
    list(result.stages).forEach(stage => stages.appendChild(stageNode(d, stage, stage.stage, ctx)));
    list(result.conditions).forEach(c => stages.appendChild(stageNode(d, c, "condition " + c.condition, ctx)));
    s.appendChild(stages);
    const checks = d.el("div", "ow-checks");
    d.add(checks, d.el("h3", "", "Quantity and price comparisons"));
    list(result.quantity_checks).forEach(check => {
      const verdict = check.agree === true ? "agree" : check.agree === false ? "disagree" : "undecided";
      const row = d.el("div", "ow-check ow-check-" + verdict);
      d.add(row, d.el("strong", "", list(check.roles).map(words).join(" vs ")),
        tagNode(d, verdict, verdict === "agree" ? "ok" : verdict === "disagree" ? "hot" : "warn"),
        check.note ? d.el("span", "ow-reason", check.note) : null);
      const values = d.el("div", "ow-check-values");
      list(check.values).forEach(value => {
        const v = d.el("div", "ow-value-row");
        d.add(v, d.el("span", "ow-prop", words(value.property)), valueNode(d, value), citationNode(d, value.citation, ctx));
        values.appendChild(v);
      });
      row.appendChild(values);
      checks.appendChild(row);
    });
    if (!list(result.quantity_checks).length) checks.appendChild(d.el("p", "ow-muted", "No comparison was answered; see the gaps for why."));
    s.appendChild(checks);
    list(result.gaps).forEach(gap => s.appendChild(gapNode(d, gap)));
    d.add(s, withheldNode(d, result.withheld));
    return s;
  }

  function gapNode(d, gap) {
    const row = d.el("div", "ow-gap ow-state-" + (STATE_CLASS[gap.state] || "warn"));
    d.add(row, tagNode(d, gap.state, STATE_CLASS[gap.state] || "warn"), d.el("strong", "", words(gap.reason)),
      plainOrEngineer(d, "ow-gap-subject", gap.subject, "What it is about, for an engineer"),
      plainOrEngineer(d, "ow-reason", gap.detail),
      engineerNode(d, "Where it came from", [gap.from]), ...searchedNodes(d, gap.searched));
    return row;
  }

  function gapsSection(d, data, ctx) {
    const gaps = data.gaps || {items: [], total: 0};
    const conflicts = list(data.conflicts);
    const s = section(d, "Gaps and conflicts", `${conflicts.length} conflicts · ${gaps.total} gaps`, "gaps");
    conflicts.forEach(conflict => {
      const row = d.el("div", "ow-conflict");
      d.add(row, tagNode(d, conflict.kind, "hot"), d.el("strong", "", humanize(conflict.subject || "")),
        conflict.note ? d.el("span", "ow-reason", humanize(conflict.note)) : null, d.el("small", "ow-muted", conflict.from || ""));
      list(conflict.values).forEach(value => {
        const v = d.el("div", "ow-value-row");
        d.add(v, d.el("span", "ow-prop", words(value.property)), valueNode(d, value), citationNode(d, value.citation, ctx));
        row.appendChild(v);
      });
      if (list(conflict.events).length) row.appendChild(d.el("small", "ow-muted", "events " + conflict.events.join(", ")));
      s.appendChild(row);
    });
    list(gaps.items).forEach(gap => s.appendChild(gapNode(d, gap)));
    if (gaps.truncated) s.appendChild(d.el("p", "ow-partial", `Showing ${gaps.items.length} of ${gaps.total} gaps.`));
    if (!conflicts.length && !gaps.total) s.appendChild(d.el("p", "ow-muted", "No gap or conflict was reported to you."));
    return s;
  }

  function intentsSection(d, data, ctx) {
    const intents = data.intents || {status: "not_configured"};
    const s = section(d, "Registered actions", intents.status === "read" ? `${list(intents.intents).length} citing this object` : words(intents.status), "intents");
    if (intents.status !== "read") {
      s.appendChild(d.el("p", "ow-muted", intents.status === "not_configured"
        ? "No action store is configured for this console." : `The action store could not be read (${intents.error || intents.status}).`));
      return s;
    }
    list(intents.intents).forEach(intent => {
      const row = d.el("div", "ow-intent");
      d.add(row, d.el("strong", "", intent.capability || "action"), tagNode(d, intent.state, "info"),
        tagNode(d, intent.outcome || "not dispatched", intent.outcome === "verified" ? "ok" : "warn"),
        d.el("span", "ow-reason", `${intent.owner_system || ""} · case ${intent.case_id} ${intent.proposal_id || ""}`));
      const approval = intent.approval || {stage: "unknown", label: "Approval state not reported"};
      const ap = d.el("p", "ow-approval ow-approval-" + approval.stage);
      d.add(ap, d.el("span", "", "Approval: "), tagNode(d, approval.stage, APPROVAL_CLASS[approval.stage]),
        d.el("span", "", ` ${approval.label} (decided in ${approval.where || "Mission Control"}, never here)`));
      row.appendChild(ap);
      list(intent.decisions).forEach(dec => {
        const line = d.el("p", "ow-decision",
          `Decision: ${words(dec.status || "?")}${dec.authorizes ? " · authorizes" : " · does not authorize"}${dec.reason_code ? " · " + words(dec.reason_code) : ""}${dec.decider_role ? " · by " + words(dec.decider_role) : ""}${dec.decided_at ? " · " : ""}`);
        if (dec.decided_at) line.appendChild(timeNode(d, dec.decided_at));
        row.appendChild(line);
      });
      if (intent.gate && (intent.gate.task_id || intent.gate.gate_id))
        row.appendChild(d.el("p", "ow-decision", `Mission Control gate: ${intent.gate.task_id || "?"} / ${intent.gate.gate_id || "not yet created"}`));
      if (list(intent.dispatches).length || list(intent.read_backs).length)
        row.appendChild(d.el("p", "ow-decision", `Dispatches: ${list(intent.dispatches).map(words).join(", ") || "none"} · read-backs: ${list(intent.read_backs).map(words).join(", ") || "none"}`));
      if (intent.production_guard) row.appendChild(d.el("p", "ow-precondition", "Production guard: " + (typeof intent.production_guard === "string" ? intent.production_guard : JSON.stringify(intent.production_guard))));
      if (intent.chain_ok === false) row.appendChild(d.el("p", "ow-error", "The action record's hash chain does not verify."));
      if (intent.next_safe_step && intent.next_safe_step.step)
        row.appendChild(d.el("p", "ow-next", `Next safe step: ${words(intent.next_safe_step.step)} (by ${intent.next_safe_step.by || "?"})${intent.next_safe_step.detail ? ": " + intent.next_safe_step.detail : ""}`));
      list(intent.human_preconditions).forEach(p => row.appendChild(d.el("p", "ow-precondition", "Needs a person: " + p)));
      list(intent.cited_records).forEach(h => row.appendChild(citationNode(d, {l1_handle: h, field_path: "evidence", lineage: ""}, ctx)));
      s.appendChild(row);
    });
    const u = intents.unmatchable || {};
    const unknown = (u.withheld || 0) + (u.chain_broken || 0);
    if (unknown) s.appendChild(d.el("p", "ow-withheld", `Across all ${intents.cases_read} case(s) you can read (not only this object), ${unknown} registered action(s) are withheld from you or fail their integrity check, so they cannot be matched to any object.`));
    if (u.cases_unreadable || u.cases_truncated || intents.cases_listing_full)
      s.appendChild(d.el("p", "ow-partial", `The action listing is partial: at most ${intents.max_cases || "?"} cases are read per open, and some cases or registrations were not read.`));
    if (!list(intents.intents).length && !unknown) s.appendChild(d.el("p", "ow-muted", "No registered action cites this object's records."));
    s.appendChild(d.el("p", "ow-note", intents.note || "Approval happens in Mission Control, never here."));
    return s;
  }

  // Cases: each case citing this object's records that the reader may read, with its
  // stored evaluation as the case workspace shows it (never evaluated here).
  const EVALUATION_TEXT = {current: "evaluation current", stale: "evaluation stale",
    unknown: "evaluation freshness unknown", withheld: "evaluation withheld", none: "not evaluated yet",
    not_configured: "no evaluation store", integrity_error: "evaluation failed its integrity check"};
  const EVALUATION_CLASS = {current: "ok", stale: "warn", unknown: "warn", withheld: "info", integrity_error: "hot"};
  function casesSection(d, data) {
    const cases = data.cases || {status: "not_configured"};
    const s = section(d, "Cases", cases.status === "read" ? `${list(cases.cases).length} citing this object` : words(cases.status), "cases");
    if (cases.status !== "read") {
      s.appendChild(d.el("p", "ow-muted", cases.status === "not_configured"
        ? "No case store is configured for this console." : `The case store could not be read (${cases.error || cases.status}).`));
      return s;
    }
    list(cases.cases).forEach(item => {
      const e = item.evaluation || {}, state = item.evaluation_status;
      const row = d.el("div", "ow-case");
      d.add(row, d.el("strong", "", item.question || item.case_id), tagNode(d, item.status, "info"),
        tagNode(d, EVALUATION_TEXT[state] || state || "evaluation not reported", EVALUATION_CLASS[state]),
        d.el("span", "ow-reason", `case ${item.case_id}`));
      if (state === "stale") row.appendChild(d.el("p", "ow-partial", `${e.freshness_reason || "The case changed after this evaluation."} Re-run the evaluation before relying on it.`));
      else if (state === "withheld") row.appendChild(d.el("p", "ow-withheld", e.reason || "Part of this case is withheld from you, so its stored evaluation is not shown."));
      if (e.status === "stored")
        row.appendChild(d.add(d.el("p", "ow-decision", `Playbook ${e.playbook_id || "?"}: ${e.passed ?? "?"} of ${e.checks ?? "?"} checks passed${list(e.failed_ids).length ? " · failed: " + list(e.failed_ids).join(", ") : ""} · recorded `), timeNode(d, e.recorded_at)));
      s.appendChild(row);
    });
    if (cases.cases_unreadable || cases.cases_listing_full)
      s.appendChild(d.el("p", "ow-partial", `The case listing is partial: at most ${cases.max_cases || "?"} cases are read per open, and some were not read.`));
    if (!list(cases.cases).length) s.appendChild(d.el("p", "ow-muted", "No case you can read cites this object's records."));
    return s;
  }

  // Originals: each cited source record with its captures (version, SHA-256 digests,
  // what cites it) and a viewer link; a capture the reader may not open says why.
  // Mail and documents come from plx.documents when the console serves it.
  // Past the server's cap the list says how many of how many, per source family.
  function originalsSection(d, data, ctx) {
    const originals = data.originals || {};
    const items = list(originals.items);
    const records = {};
    items.forEach(item => { (records[item.record || item.l1_handle] = records[item.record || item.l1_handle] || []).push(item); });
    const names = Object.keys(records).sort();
    const s = section(d, "Originals", `${names.length} source records · ${items.length} cited captures${originals.truncated ? " · partial" : ""}`, "originals");
    if (originals.truncated) {
      const families = Object.keys(originals.by_scheme || {}).sort()
        .map(scheme => `${scheme} ${originals.by_scheme[scheme].shown} of ${originals.by_scheme[scheme].total}`);
      const held = originals.withheld;
      const heldNote = !held || !held.total ? "" : held.shown === held.total
        ? "; every withheld capture is listed"
        : `; ${held.shown} of ${held.total} withheld captures listed, the rest only counted`;
      s.appendChild(d.el("p", "ow-partial", `Partial: showing ${items.length} of ${originals.total} cited captures (${families.join(", ")})${heldNote}.`));
    }
    names.forEach(name => {
      const versions = records[name];
      const box = d.el("div", "ow-original");
      box.setAttribute("data-record", name);
      const address = d.el("details", "engineer");
      d.add(address, d.el("summary", "", "Record address"), d.el("code", "ow-record-name", name));
      d.add(box, tagNode(d, versions[0].scheme), d.el("strong", "", `${versions[0].source || versions[0].scheme || "Source"} record`), address,
        d.el("span", "ow-system", "from " + (versions[0].source || versions[0].scheme || "unknown source") +
          (list(versions[0].lineages).length ? " · " + lineageWords(list(versions[0].lineages).join(",")) : "")),
        versions.length > 1 ? tagNode(d, `${versions.length} versions cited`, "warn") : null);
      versions.forEach(item => {
        const v = d.el("div", "ow-version");
        d.add(v, d.el("span", "ow-version-label", "version " + (item.version || "unversioned")));
        if (item.withheld) {
          d.add(v, d.el("span", "ow-withheld-inline", `withheld: ${item.withheld.reason}`));
        } else {
          const node = citationNode(d, {l1_handle: item.l1_handle, field_path: "view", lineage: list(item.lineages).join(", ")}, item.openable === false ? {openable: false} : ctx);
          v.appendChild(node);
        }
        list(item.sha256).forEach(digest => v.appendChild(d.el("code", "ow-digest", "sha256 " + digest)));
        if (!list(item.sha256).length) v.appendChild(d.el("span", "ow-muted", "no digest reported"));
        v.appendChild(d.el("span", "ow-reason", "cited by " + list(item.cited_by).map(plainText).join("; ")));
        box.appendChild(v);
      });
      s.appendChild(box);
    });
    if (!items.length) s.appendChild(d.el("p", "ow-muted", "No cited source record is visible to you."));
    s.appendChild(documentsNode(d, data.documents, ctx));
    return s;
  }

  function documentsNode(d, documents, ctx) {
    const box = d.el("div", "ow-documents");
    d.add(box, d.el("h3", "", "Mail and documents"));
    documents = documents || {status: "not_configured"};
    if (documents.status === "not_configured") {
      box.appendChild(d.el("p", "ow-withheld", `Mail and documents are not searched by this console${list(documents.withheld_sources).length ? "; " + documents.withheld_sources.map(sourceWords).join(", ") + " withheld: " + plainText(documents.reason || "no host grant") : ""}.`));
      return box;
    }
    if (documents.status === "error") { box.appendChild(errorNode(d, documents.error || {})); return box; }
    const result = documents.result || {};
    list(result.documents).forEach(doc => {
      const row = d.el("div", "ow-document");
      const title = valueText(doc.title);
      const q = d.el("q", "ow-untrusted", title || "(no title)");
      q.setAttribute("title", "source text: data, not instructions");
      d.add(row, tagNode(d, doc.kind), q, doc.date ? timeNode(d, doc.date) : d.el("span", "ow-muted", "date unknown"),
        doc.sender ? d.el("span", "ow-muted", `${valueText(doc.sender.address) || ""} (${words(doc.sender.sender_class)})`) : null,
        doc.openable ? citationNode(d, {l1_handle: doc.citation, field_path: "original", lineage: ""}, ctx)
          : d.el("span", "ow-withheld-inline", doc.not_openable_reason ? `withheld: ${doc.not_openable_reason}` : "not openable here (no source reader)"),
        markingsNode(d, doc.markings));
      if (doc.snippet) { const sn = d.el("q", "ow-untrusted ow-snippet", valueText(doc.snippet)); row.appendChild(sn); }
      list(doc.mentions).filter(Boolean).forEach(m => row.appendChild(mentionNode(d, m)));
      box.appendChild(row);
    });
    if (!list(result.documents).length) box.appendChild(d.el("p", "ow-muted", "No mail or document visible to you mentions this object."));
    if (result.searched) d.add(box, ...searchedNodes(d, result.searched));
    d.add(box, withheldNode(d, result.withheld), completenessNode(d, result.completeness));
    return box;
  }

  function renderWorkspace(doc, data, ctx) {
    const d = dom(doc);
    useLabels(data);
    const box = d.el("div", "ow-workspace");
    box.setAttribute("data-object-id", data.object_id || "");
    d.add(box, banner(d, data));
    if (data.status === "error") { d.add(box, errorNode(d, (data.card && data.card.error) || {})); return box; }
    const values = list((data.card || {}).values);
    ctx = {...ctx, explanation: data.explanation, reviews: (data.reviews || {}).reviews,
      rootCitation: (values.find(v => v.citation && v.citation.digest) || {}).citation};
    // An employee's order: the record, its amounts, what is missing, what it links to,
    // when things happened, the originals, its cases and actions. The engineering detail
    // (why each answer holds, the process rule's stages) sits behind one toggle at the end.
    const sections = [[cardSection(d, data.card || {}, ctx), "Card"], [amountsSection(d, data, ctx), "Amounts"],
      [allocationsSection(d, data, ctx), "Allocations"], [gapsSection(d, data, ctx), "Gaps"],
      [aroundSection(d, doc, data, ctx), "Linked records"], [timelineSection(d, data, ctx), "Timeline"],
      [originalsSection(d, data, ctx), "Originals"], [casesSection(d, data), "Cases"], [intentsSection(d, data, ctx), "Actions"]]
      .filter(([node]) => node);
    const technical = technicalDetail(d, [explanationSection(d, data.explanation, ctx), processSection(d, data, ctx)]);
    // The jump buttons scroll to a section; they never touch the address, which holds the
    // page route (app.js), so a reload or Back keeps the open record.
    const nav = d.el("nav", "ow-jump");
    nav.setAttribute("aria-label", "Jump to a part of this record");
    const jump = (label, target, before) => {
      const button = d.el("button", "ow-jump-button", label);
      button.type = "button";
      button.setAttribute("data-jump", target.getAttribute("data-section") || "technical");
      button.addEventListener("click", () => {
        if (before) before();
        if (target.scrollIntoView) target.scrollIntoView({block: "start", behavior: "smooth"});
      });
      nav.appendChild(button);
    };
    // The status card first: what is wrong, why and what to do next, before the evidence.
    const statusCard = data.status_card ? renderStatusCard(doc, data.status_card, ctx) : null;
    if (statusCard) jump("Status", statusCard);
    sections.forEach(([node, label]) => jump(label, node));
    if (technical) jump("Technical detail", technical, () => { technical.open = true; });
    d.add(box, nav, d.el("div", "ow-source-slot"));
    if (statusCard) d.add(box, statusCard);
    if (data.ask && data.ask.served) d.add(box, extensionSlot(d, ctx, "ask", {kind: "object", object_id: data.object_id}));
    sections.forEach(([node]) => box.appendChild(node));
    d.add(box, extensionSlot(d, ctx, "follow_up", {kind: "object", object_id: data.object_id,
      cases: (data.cases || {}).status === "read" ? list(data.cases.cases) : [], case_id: ctx.caseId || null}), technical);
    return box;
  }

  // The engineering detail behind an answer (why it holds, the process rule's stages) in
  // one closed toggle; null when there is none.
  function technicalDetail(d, nodes) {
    const items = nodes.filter(Boolean);
    if (!items.length) return null;
    const box = d.el("details", "engineer ow-tech");
    box.setAttribute("data-technical", "");
    d.add(box, d.el("summary", "", "Technical detail: why this answer holds" + (items.some(n => n.getAttribute && n.getAttribute("data-section") === "process") ? ", process stages" : "")), ...items);
    return box;
  }

  // ---- status card (/api/status-card, operating/status_card.py) -------------------
  // At most five ranked blockers, each with its citations and a next step (who, what);
  // cited facts; the record's copies in one line; each unknown once. The card's words
  // are the server's plain words; a source value (the record's label, a fact's value)
  // is data and set as text. Rendered first in the object view and on a case.
  const CARD_STATUS = {not_found: "No status card: the record is not found in what you can read.",
    ambiguous: "No status card: more than one record matches; open one to see its card.",
    unsupported: "No status card for this kind of record.", invalid_input: "No status card: the reference is not usable.",
    error: "No status card: the read failed."};
  function cardCitations(d, citations, ctx) {
    const box = d.el("span", "ow-sc-cites");
    // A host view (a case) may draw its own citation chip; otherwise the workspace's.
    list(citations).forEach(c => box.appendChild(ctx && typeof ctx.citationNode === "function" ? ctx.citationNode(c)
      : citationNode(d, {l1_handle: c.handle, field_path: c.field}, ctx)));
    return box;
  }
  function renderStatusCard(doc, card, ctx) {
    const d = dom(doc);
    const box = d.el("section", "ow-section status-card");
    box.setAttribute("data-section", "status");
    box.setAttribute("aria-label", "Status card");
    if (!card || card.status !== "read") {
      d.add(box, d.el("p", "ow-sc-none", CARD_STATUS[card && card.status] || CARD_STATUS.error));
      return box;
    }
    const head = d.el("div", "ow-section-head");
    d.add(head, d.el("h2", "", `Status · ${card.type_label || "record"} ${valueText(card.label) || ""}`.trim()),
      d.el("small", "", `as of ${card.as_of || "today"}`));
    d.add(box, head, d.el("p", "ow-sc-headline", card.headline || ""));
    const blockers = list(card.blockers);
    if (blockers.length) {
      const ol = d.el("ol", "ow-sc-blockers");
      blockers.forEach(b => {
        const li = d.el("li", "ow-sc-blocker");
        li.setAttribute("data-kind", b.kind || "");
        const step = b.next_step || {};
        d.add(li, d.el("strong", "", b.title || ""), d.el("p", "", b.why || ""),
          d.el("p", "ow-sc-next", `Next: ${list(step.who).join(" or ") || "the owner of the record"}: ${step.what || ""}`),
          cardCitations(d, b.citations, ctx));
        ol.appendChild(li);
      });
      box.appendChild(ol);
      if (card.more_blockers) box.appendChild(d.el("p", "ow-sc-more", `${card.more_blockers} more not shown.`));
    }
    const facts = list(card.facts);
    if (facts.length) {
      const dl = d.el("dl", "ow-sc-facts");
      facts.forEach(f => {
        const dd = d.el("dd", "");
        d.add(dd, d.el("span", "ow-typed", valueText(f.value) === null ? "empty in source" : valueText(f.value)),
          f.currency ? d.el("span", "ow-unit", f.currency) : null, cardCitations(d, f.citations, ctx));
        d.add(dl, d.el("dt", "", f.text || ""), dd);
      });
      box.appendChild(dl);
    }
    if (card.copies) {
      const p = d.el("p", "ow-sc-copies", card.copies.text || "");
      d.add(p, cardCitations(d, card.copies.citations, ctx));
      box.appendChild(p);
    }
    const unknowns = list(card.unknowns);
    if (unknowns.length) {
      const ul = d.el("ul", "ow-sc-unknowns");
      unknowns.forEach(u => { const li = d.el("li", "", u.text || ""); d.add(li, cardCitations(d, u.citations, ctx)); ul.appendChild(li); });
      d.add(box, d.el("h3", "", "Not known"), ul);
    }
    const questions = list(card.questions);
    if (questions.length && ctx && ctx.onInvestigate && card.object_id) {
      const row = d.el("div", "ow-sc-questions");
      questions.forEach(q => {
        const button = d.el("button", "button subtle", q.title || q.family);
        button.type = "button";
        button.addEventListener("click", () => ctx.onInvestigate(q.family, card.object_id));
        row.appendChild(button);
      });
      box.appendChild(row);
    }
    return box;
  }

  // ---- one source record ------------------------------------------------------------

  function renderSource(doc, data, ctx) {
    const d = dom(doc);
    const box = d.el("section", "ow-source");
    const head = d.el("div", "ow-section-head");
    const close = d.el("button", "ow-link-button", "Close ×");
    close.type = "button";
    close.addEventListener("click", () => ctx && ctx.onClose && ctx.onClose());
    d.add(head, d.el("h2", "", data.kind === "original" ? "Original" : "Source record"), close);
    d.add(box, head);
    if (data.note) box.appendChild(d.el("p", "ow-note", data.note));
    if (data.status && data.status.status) box.appendChild(d.el("p", "ow-muted", `Source status: ${words(data.status.status)}${data.status.changed_since ? " · the record changed since this capture" : ""}`));
    const rows = data.row ? [data.row] : list(data.rows);
    // Where the record lives (its handle, relation and key, the capture and mirror
    // generation) is an engineer's detail.
    d.add(box, engineerNode(d, "Record address", [data.handle || data.citation || "",
      data.relation ? `${data.relation} ${JSON.stringify(data.key || "")}` : null,
      data.capture_id !== undefined && data.capture_id !== null ? `Capture ${data.capture_id}${data.status && data.status.mirror_generation ? " of mirror generation " + data.status.mirror_generation : ""}` : null]));
    rows.forEach(row => {
      const dl = d.el("dl", "ow-record");
      Object.keys(row || {}).forEach(key => { d.add(dl, d.el("dt", "", key), d.el("dd", "", row[key] === null ? "—" : typeof row[key] === "object" ? JSON.stringify(row[key]) : String(row[key]))); });
      box.appendChild(dl);
    });
    if (data.record && !rows.length) {
      const dl = d.el("dl", "ow-record");
      Object.keys(data.record).forEach(key => { d.add(dl, d.el("dt", "", key), d.el("dd", "", typeof data.record[key] === "object" ? JSON.stringify(data.record[key]) : String(data.record[key]))); });
      box.appendChild(dl);
    }
    if (data.kind === "original") {
      box.appendChild(d.el("p", "ow-muted", `${data.filename || ""} · sha256 ${data.sha256 || "?"} (re-hashed on this read)`));
      if (typeof data.text === "string") box.appendChild(d.el("pre", "ow-text", data.text || "(no text)"));
    }
    if (data.kind === "source_read") {
      // A mail, SharePoint or BC record read through the case host (plx.read): its
      // fields and text are source data; its labels (as of capture, OCR) are gaps.
      box.appendChild(d.el("p", "ow-muted", `${words(data.status)}${data.lineage ? " · " + lineageWords(data.lineage) : ""}${data.digest ? " · digest " + data.digest : ""}`));
      const fields = data.fields || {};
      if (Object.keys(fields).length) {
        const dl = d.el("dl", "ow-record");
        Object.keys(fields).forEach(key => d.add(dl, d.el("dt", "", key), d.el("dd", "", valueText(fields[key]) === null ? "—" : valueText(fields[key]))));
        box.appendChild(dl);
      }
      const text = valueText(data.content);
      if (text !== null) box.appendChild(d.el("pre", "ow-text", text || "(no text)"));
      if (data.text_window && data.text_window.next_offset !== null && data.text_window.next_offset !== undefined)
        box.appendChild(d.el("p", "ow-partial", `Showing part of the text; it continues at character ${data.text_window.next_offset}.`));
      list(data.gaps).forEach(gap => box.appendChild(gapNode(d, gap)));
    }
    return box;
  }

  // ---- mount (browser) -------------------------------------------------------------------

  function mount(target, options) {
    const doc = options.doc || root.document;
    const api = options.api;
    const d = dom(doc);
    d.clear(target);
    // The objective box: a question in the employee's words. The server finds the
    // objects it names (plx.find) and lists the question families each can answer; the
    // employee picks one (nothing is chosen for them).
    const objective = d.el("form", "ow-objective");
    const question = d.el("textarea");
    question.setAttribute("rows", "2");
    question.setAttribute("placeholder", "What do you need to find out? e.g. Is purchase order 1234 fully received and invoiced?");
    question.setAttribute("aria-label", "Describe what you need to find out");
    const ask = d.el("button", "button", "Start");
    ask.type = "submit";
    d.add(objective, d.el("label", "ow-objective-label", "Start from a question"), question, ask);
    const form = d.el("form", "ow-search");
    const input = d.el("input");
    input.setAttribute("type", "search");
    input.setAttribute("placeholder", "Order, PO, invoice, item, customer… any identifier");
    input.setAttribute("aria-label", "Find a business object");
    const go = d.el("button", "button subtle", "Find");
    go.type = "submit";
    d.add(form, input, go);
    const results = d.el("div", "ow-results-slot");
    const view = d.el("div", "ow-view-slot");
    const status = d.el("p", "ow-status");
    status.setAttribute("role", "status");
    d.add(target, objective, form, status, results, view);
    const state = {history: []};
    const post = options.post;
    const controls = typeof post === "function" ? {
      review: (dd, subject) => subject.kind === "link" ? reviewControl(dd, subject, post) : null,
      value_review: (dd, subject) => subject.kind === "value" ? valueReviewControl(dd, subject, post) : null,
      allocate: (dd, subject) => subject.kind === "allocation" ? allocateControl(dd, subject, post) : null,
      follow_up: (dd, subject) => subject.object_id ? followUpControl(dd, subject, post) : null,
      record_on_case: (dd, subject) => subject.family && subject.object_id ? recordOnCaseControl(dd, subject, post, api, ctx) : null,
      ask: (dd, subject) => subject.object_id ? askControl(dd, subject, post, api, ctx) : null} : {};
    // options.caseId: the case the reader opened this view from (its follow-ups go there).
    const ctx = {openable: false, caseId: typeof options.caseId === "string" ? options.caseId : null,
      extensions: {...controls, ...(options.extensions || {})}};
    function fail(error) { status.textContent = plainError(error); }
    // The route: what this view shows ({q, asked} for the find, then {object} or
    // {family, root}). A person's action reports it through options.onRoute so the page can
    // keep it in its address (reload, Back, a pasted link); applyRoute shows a route again
    // without reporting it.
    const route = {};
    const onRoute = typeof options.onRoute === "function" ? options.onRoute : null;
    const report = quiet => { if (!quiet && onRoute) onRoute({...route}); };
    let answerSlot = null;
    const clearAnswer = () => { if (answerSlot && answerSlot.parentNode) answerSlot.parentNode.removeChild(answerSlot); answerSlot = null; };
    const reveal = node => { if (node && typeof node.scrollIntoView === "function") node.scrollIntoView({block: "start"}); };
    // The find result's row for one object (the plan answer renders right under it).
    const rowOf = objectId => {
      const stack = [results];
      while (stack.length) {
        const node = stack.shift();
        const kids = Array.from(node.children || []);
        for (const kid of kids) {
          if (String(kid.className || "").split(" ").includes("ow-candidate-row") && kid.getAttribute && kid.getAttribute("data-object-id") === objectId) return kid;
          stack.push(kid);
        }
      }
      return null;
    };
    ctx.onSource = async handle => {
      try {
        const data = await api(SOURCE_ROUTE + encodeURIComponent(handle));
        const host = answerSlot && answerSlot.parentNode ? answerSlot : view.firstChild;
        const slot = host && host.querySelector ? host.querySelector(".ow-source-slot") : null;
        const panel = renderSource(doc, data, {onClose: () => slot && d.clear(slot)});
        if (slot) { d.clear(slot).appendChild(panel); if (panel.scrollIntoView) panel.scrollIntoView({block: "nearest"}); }
      } catch (error) { fail(error); }
    };
    ctx.onOpen = async (objectId, quiet) => {
      if (!objectId) return;
      status.textContent = "Reading…";
      try {
        const [data, card] = await Promise.all([api("/api/object?id=" + encodeURIComponent(objectId)),
          api(STATUS_CARD_ROUTE + "?id=" + encodeURIComponent(objectId)).catch(() => null)]);
        ctx.openable = Boolean(data.sources_openable);
        if (card && data.status !== "error") data.status_card = card;
        state.history.push(objectId);
        clearAnswer();
        const panel = renderWorkspace(doc, data, ctx);
        d.clear(view).appendChild(panel);
        delete route.family; delete route.root;
        route.object = objectId;
        report(quiet);
        if (!quiet) reveal(panel);
        status.textContent = "";
      } catch (error) { fail(error); }
    };
    // A plan's answer renders where the plan was clicked (under its find row) and scrolls
    // into view; without that row (a pasted link to an answer) it takes the view slot.
    ctx.onInvestigate = async (family, objectId, quiet) => {
      status.textContent = "Investigating…";
      try {
        const data = await api(`${INVESTIGATE_ROUTE}?family=${encodeURIComponent(family)}&root=${encodeURIComponent(objectId)}`);
        ctx.openable = Boolean(data.sources_openable);
        const panel = renderInvestigation(doc, data, ctx);
        const slot = d.el("div", "ow-workspace");
        d.add(slot, panel, d.el("div", "ow-source-slot"));
        clearAnswer();
        d.clear(view);
        const row = rowOf(objectId);
        if (row) {
          answerSlot = d.el("div", "ow-answer-slot");
          answerSlot.setAttribute("data-family", family || "");
          d.add(answerSlot, slot);
          row.appendChild(answerSlot);
        } else view.appendChild(slot);
        delete route.object;
        route.family = family; route.root = objectId;
        report(quiet);
        reveal(row ? answerSlot : slot);
        status.textContent = "";
      } catch (error) { fail(error); }
    };
    async function search(query, asked, quiet) {
      if (!query) return;
      status.textContent = "Searching…";
      try {
        const data = await api("/api/objects?q=" + encodeURIComponent(query));
        if (data.available === false) { status.textContent = "Object workspace not configured: start the console with --ontology-store or --project-ontology."; return; }
        d.clear(results);
        clearAnswer();
        d.clear(view);
        if (asked) results.appendChild(d.el("p", "ow-asked", `Your question: ${query}`));
        results.appendChild(renderFind(doc, data, ctx));
        Object.keys(route).forEach(key => delete route[key]);
        route.q = query;
        if (asked) route.asked = "1";
        report(quiet);
        status.textContent = "";
      } catch (error) { fail(error); }
    }
    // Show a route again (a reload, Back/Forward, a pasted link) without reporting it.
    // Only strings are read; anything else in the route is ignored.
    ctx.applyRoute = async next => {
      const want = {};
      ["q", "asked", "object", "family", "root"].forEach(key => {
        if (next && typeof next[key] === "string" && next[key]) want[key] = next[key];
      });
      if (want.q && (want.q !== route.q || Boolean(want.asked) !== Boolean(route.asked))) {
        if (want.asked) question.value = want.q; else input.value = want.q;
        await search(want.q, Boolean(want.asked), true);
      } else if (!want.q && route.q) {
        Object.keys(route).forEach(key => delete route[key]);
        d.clear(results); clearAnswer(); d.clear(view);
      }
      if (want.object) {
        if (want.object !== route.object) await ctx.onOpen(want.object, true);
      } else if (want.family && want.root) {
        if (want.family !== route.family || want.root !== route.root) await ctx.onInvestigate(want.family, want.root, true);
      } else if (route.object || route.family) {
        delete route.object; delete route.family; delete route.root;
        clearAnswer(); d.clear(view);
      }
      return {...route};
    };
    ctx.route = () => ({...route});
    objective.addEventListener("submit", event => {
      if (event && event.preventDefault) event.preventDefault();
      return search(String(question.value || "").trim(), true);
    });
    form.addEventListener("submit", event => {
      if (event && event.preventDefault) event.preventDefault();
      return search(String(input.value || "").trim(), false);
    });
    if (options.route) ctx.ready = ctx.applyRoute(options.route);
    return ctx;
  }

  const api = {sourceHref, valueText, labelOf, humanize, plainError, renderFind, renderWorkspace, renderInvestigation, renderSource, renderAnswer, renderStatusCard, mount, unitToken, notPreparedWords, etTime, TIER_CLASS};
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  root.PLXWorkspace = api;

})(typeof globalThis !== "undefined" ? globalThis : this);
