"use strict";
// The CI workflow (.github/workflows/desktop.yml) against what it runs: every build script it
// names exists, a signed run passes every variable the signing preflight checks, and the
// artifacts it uploads are the files electron-builder writes. (A text check: no YAML parser in node.)
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { NEEDED } = require("../scripts/check-signing.js");

const WORKFLOW = path.join(__dirname, "../../.github/workflows/desktop.yml");
const pkg = JSON.parse(fs.readFileSync(path.join(__dirname, "../package.json"), "utf8"));

test("the CI workflow builds with scripts that exist and signs with every checked variable", { skip: !fs.existsSync(WORKFLOW) }, () => {
  const wf = fs.readFileSync(WORKFLOW, "utf8");
  assert.doesNotMatch(wf, /\t/, "YAML must not contain tabs");
  const dists = [...wf.matchAll(/\{ name: (\w+), os: [\w.-]+, dist: "([\w:]+)" \}/g)].map((m) => [m[1], m[2]]);
  assert.deepEqual(dists.map((d) => d[0]), ["linux", "windows", "mac"]);
  for (const [name, script] of dists) {
    assert.ok(pkg.scripts[script], `${script} is not a package.json script`);
    if (name !== "linux") assert.ok(pkg.scripts[`${script}:signed`], `${script}:signed is not a package.json script`);
  }
  const signedStep = wf.slice(wf.indexOf("name: Build (signed)"), wf.indexOf("name: Smoke test the packaged app"));
  for (const v of [...NEEDED.win, ...NEEDED.mac]) {
    assert.match(signedStep, new RegExp(`\\b${v}: \\$\\{\\{ secrets\\.${v} \\}\\}`), `${v} is not passed to the signed build`);
  }
  // unsigned by default: no secret reaches any other step
  assert.equal((wf.match(/secrets\./g) || []).length, (signedStep.match(/secrets\./g) || []).length);
  assert.match(wf, /CSC_IDENTITY_AUTO_DISCOVERY: "false"/);
  // never publishes or deploys
  assert.doesNotMatch(wf, /--publish (always|onTag|onTagOrDraft)|contents: write/);
  for (const ext of ["AppImage", "tar.gz", "exe", "dmg", "zip"]) assert.match(wf, new RegExp(`desktop/dist/\\*\\.${ext.replace(".", "\\.")}`));
});
