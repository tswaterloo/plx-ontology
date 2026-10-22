"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const badge = {textContent: ""};
const requests = [];
const context = vm.createContext({
  document: {
    addEventListener() {},
    getElementById(id) { assert.equal(id, "objective-count"); return badge; },
  },
  encodeURIComponent,
  fetch: async url => {
    requests.push(url);
    assert.equal(url, "/api/objectives?cursor=page-one");
    return {ok: true, status: 200, json: async () => ({
      available: true, capabilities: [{id: "existing"}],
      runs: [{objective_id: "obj-second", status: "needs_human"},
             {objective_id: "obj-first", status: "needs_human"}],
      next_cursor: null, has_more: false, page_limit: 8,
    })};
  },
});
const app = fs.readFileSync(path.join(__dirname, "../operator_ui/app.js"), "utf8");
vm.runInContext(app, context, {filename: "app.js"});

(async () => {
  context.button = {disabled: false, textContent: "Load more objectives →"};
  await vm.runInContext(`state.view = "overview";
    state.objectives = {available: true, capabilities: [{id: "existing"}],
      runs: [{objective_id: "obj-first", status: "needs_human"}],
      next_cursor: "page-one"};
    loadMoreObjectives(button)`, context);
  const result = vm.runInContext(`({runs: state.objectives.runs.map(x => x.objective_id),
    next_cursor: state.objectives.next_cursor,
    capabilities: state.objectives.capabilities.map(x => x.id)})`, context);
  assert.deepEqual(Array.from(result.runs), ["obj-first", "obj-second"]);
  assert.deepEqual(Array.from(result.capabilities), ["existing"]);
  assert.equal(result.next_cursor, null);
  assert.equal(badge.textContent, 2);
  assert.deepEqual(requests, ["/api/objectives?cursor=page-one"]);
  console.log("objective UI pagination: passed");
})().catch(error => { console.error(error); process.exitCode = 1; });
