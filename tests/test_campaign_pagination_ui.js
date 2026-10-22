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
    getElementById(id) { assert.equal(id, "campaign-count"); return badge; },
  },
  encodeURIComponent,
  fetch: async url => {
    requests.push(url);
    assert.equal(url, "/api/campaigns?cursor=page-one");
    return {ok: true, status: 200, json: async () => ({
      runs: [{campaign_id: "camp-second"}, {campaign_id: "camp-first"}],
      next_cursor: null, has_more: false, page_limit: 8,
    })};
  },
});
vm.runInContext(fs.readFileSync(path.join(__dirname, "../operator_ui/app.js"), "utf8"),
  context, {filename: "app.js"});

(async () => {
  context.button = {disabled: false, textContent: "Load more campaigns →"};
  await vm.runInContext(`state.view = "overview";
    state.campaigns = {intents: {Customer: "customer_operations"},
      runs: [{campaign_id: "camp-first"}], next_cursor: "page-one"};
    loadMoreCampaigns(button)`, context);
  const result = vm.runInContext(`({runs: state.campaigns.runs.map(x => x.campaign_id),
    next_cursor: state.campaigns.next_cursor, intents: state.campaigns.intents})`, context);
  assert.deepEqual(Array.from(result.runs), ["camp-first", "camp-second"]);
  assert.equal(result.next_cursor, null);
  assert.equal(result.intents.Customer, "customer_operations");
  assert.equal(badge.textContent, 2);
  assert.deepEqual(requests, ["/api/campaigns?cursor=page-one"]);
  console.log("campaign UI pagination: passed");
})().catch(error => { console.error(error); process.exitCode = 1; });
