const test = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");
const { spawnSync } = require("node:child_process");
const { NEEDED, missing } = require("../scripts/check-signing.js");

test("a signed build names every missing variable and fails closed", () => {
  assert.deepStrictEqual(missing("mac", {}), NEEDED.mac);
  assert.deepStrictEqual(missing("mac", { PLX_MAC_IDENTITY: "x", APPLE_ID: " ", APPLE_APP_SPECIFIC_PASSWORD: "p", APPLE_TEAM_ID: "t" }), ["APPLE_ID"]);
  assert.throws(() => missing("linux", {}));
  const run = spawnSync("node", [path.join(__dirname, "../scripts/check-signing.js"), "win"], { env: { PATH: process.env.PATH, AZURE_CLIENT_SECRET: "s3cret" }, encoding: "utf8" });
  assert.strictEqual(run.status, 1);
  assert.match(run.stderr, /AZURE_TENANT_ID/);
  assert.doesNotMatch(run.stderr + run.stdout, /s3cret/);
});

test("every variable the signed config reads is one the preflight checks, and the plain config stays unsigned", () => {
  const signed = fs.readFileSync(path.join(__dirname, "../electron-builder.signed.yml"), "utf8");
  const used = [...signed.matchAll(/\$\{env\.(\w+)\}/g)].map((m) => m[1]);
  const checked = new Set([...NEEDED.win, ...NEEDED.mac]);
  assert.ok(used.length >= 5);
  for (const v of used) assert.ok(checked.has(v), `${v} is read but not checked`);
  const plain = fs.readFileSync(path.join(__dirname, "../electron-builder.yml"), "utf8");
  assert.match(plain, /identity: null/);
  assert.doesNotMatch(plain, /azureSignOptions:/);
  assert.match(plain, /signExecutable: false/);
  assert.match(signed, /signExecutable: true/);
  assert.ok(fs.existsSync(path.join(__dirname, "../build/entitlements.mac.plist")));
});
