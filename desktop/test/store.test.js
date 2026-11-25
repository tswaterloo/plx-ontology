"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { createStore, validKey, TTL_MS } = require("../src/store");
const { answerSeen, handlers, senderOk } = require("../src/store-ipc");

// a sealer that only the "OS" (this test's secret) can open: stands in for safeStorage
const SECRET = crypto.randomBytes(32);
const sealer = (over = {}) => ({
  async available() { return true; },
  async seal(buf) {
    const iv = crypto.randomBytes(12);
    const c = crypto.createCipheriv("aes-256-gcm", SECRET, iv);
    const ct = Buffer.concat([c.update(buf), c.final()]);
    return Buffer.concat([iv, c.getAuthTag(), ct]);
  },
  async unseal(b) {
    const d = crypto.createDecipheriv("aes-256-gcm", SECRET, b.subarray(0, 12));
    d.setAuthTag(b.subarray(12, 28));
    return { key: Buffer.concat([d.update(b.subarray(28)), d.final()]) };
  },
  ...over,
});
const tmp = () => fs.mkdtempSync(path.join(os.tmpdir(), "plx-store-"));
const walk = (d) => (fs.existsSync(d) ? fs.readdirSync(d, { recursive: true }).map((f) => path.join(d, f)).filter((f) => fs.statSync(f).isFile()) : []);
const answer = (store, door) => store.observe({ refused: false, door });
async function put(store, key, body, door, extra = {}) {
  answer(store, door);
  return store.put({ key, body, door, release: "r1", fetchedAt: Date.now(), ...extra });
}

test("an answer kept comes back after a restart, and only as the same opaque string", async () => {
  const dir = tmp();
  const a = createStore({ dir, sealer: sealer() });
  await a.open();
  const body = JSON.stringify({ items: [1, 2, 3], note: "Croda late PO 12345" });
  assert.equal(await put(a, "/api/late?x=1", body, "doorA"), true);
  a.flush();
  const b = createStore({ dir, sealer: sealer() });
  const first = await b.open();
  assert.equal(first.durable, true);
  assert.equal(first.door, "doorA");
  assert.deepEqual(first.keys, ["/api/late?x=1"]);
  assert.equal(first.entries[0].body, body);
  assert.equal((await b.get("/api/late?x=1")).body, body);
  assert.equal(await b.get("/api/other"), null);
});

test("nothing readable is on disk: no body, no record id in a file name, files private", async () => {
  const dir = tmp();
  const s = createStore({ dir, sealer: sealer() });
  await s.open();
  await put(s, "/api/object/PurchaseOrder/PO-998877", '{"supplier":"Acme Secret Ltd"}', "d1");
  s.flush();
  for (const f of walk(dir)) {
    const raw = fs.readFileSync(f);
    assert.equal(raw.includes("Acme Secret"), false, f);
    assert.equal(raw.includes("PO-998877"), false, f);
    assert.equal(path.basename(f).includes("998877"), false, f);
    if (process.platform !== "win32") assert.equal(fs.statSync(f).mode & 0o077, 0, `${f} readable by others`);
  }
  assert.ok(walk(dir).length >= 3); // key, index, one entry
});

test("a door that differs wipes the store before the answer is delivered; the old person's data cannot be drawn", async () => {
  const dir = tmp();
  const s = createStore({ dir, sealer: sealer() });
  await s.open();
  await put(s, "/api/a", '{"mine":1}', "doorA");
  await put(s, "/api/b", '{"mine":2}', "doorA");
  assert.equal(s.size, 2);
  const out = answer(s, "doorB"); // person B's first answer on this machine
  assert.equal(out, "wiped");
  assert.equal(s.size, 0);
  assert.equal(await s.get("/api/a"), null);
  assert.deepEqual(walk(dir), []); // key, index and entries are gone from disk
  // a restart finds nothing either
  const again = createStore({ dir, sealer: sealer() });
  assert.deepEqual((await again.open()).entries, []);
  // B's own answer is kept under B's door
  assert.equal(await put(s, "/api/a", '{"theirs":1}', "doorB"), true);
  assert.equal((await s.get("/api/a")).body, '{"theirs":1}');
});

test("the same door, however many answers and refreshes, never wipes", async () => {
  const s = createStore({ dir: tmp(), sealer: sealer() });
  await s.open();
  await put(s, "/api/a", "{}", "d");
  for (let i = 0; i < 50; i++) assert.equal(answer(s, "d"), "kept");
  assert.equal(s.size, 1);
  assert.equal(s.observe({ refused: false, door: null }), "kept"); // an answer with no door value says nothing
  assert.equal(s.observe({ refused: false, door: "" }), "kept");
  assert.equal(s.size, 1);
});

test("a refused session, sign-out and a changed server each wipe", async () => {
  for (const how of ["refused", "signout", "server"]) {
    const dir = tmp();
    const s = createStore({ dir, sealer: sealer() });
    await s.open();
    await put(s, "/api/a", "{}", "d");
    if (how === "refused") assert.equal(answerSeen(s, "https://wb.example", { url: "https://wb.example/api/me", method: "GET", statusCode: 401, responseHeaders: { "X-PLX-Session": ["refused"] } }), "wiped");
    if (how === "signout") assert.equal(answerSeen(s, "https://wb.example", { url: "https://wb.example/api/auth/logout", method: "POST", statusCode: 500, responseHeaders: {} }), "wiped");
    if (how === "server") s.wipe("server changed");
    assert.equal(s.size, 0, how);
    assert.deepEqual(walk(dir), [], how);
  }
});

test("an ordinary 403 or 401 without the refused marker keeps the store", async () => {
  const s = createStore({ dir: tmp(), sealer: sealer() });
  await s.open();
  await put(s, "/api/a", "{}", "d");
  const o = "https://wb.example";
  for (const statusCode of [403, 401, 500, 200]) {
    const out = answerSeen(s, o, { url: `${o}/api/ops/alarms`, method: "GET", statusCode, responseHeaders: { "x-plx-door": ["d"] } });
    assert.equal(out, "kept", String(statusCode));
  }
  assert.equal(s.size, 1);
});

test("only the Workbench's own API answers count; another origin's headers change nothing", async () => {
  const s = createStore({ dir: tmp(), sealer: sealer() });
  await s.open();
  await put(s, "/api/a", "{}", "d");
  const hdrs = { "X-PLX-Session": ["refused"], "X-PLX-Door": ["zzz"] };
  assert.equal(answerSeen(s, "https://wb.example", { url: "https://evil.example/api/x", method: "GET", statusCode: 401, responseHeaders: hdrs }), null);
  assert.equal(answerSeen(s, "https://wb.example", { url: "https://wb.example/not-api", method: "GET", statusCode: 401, responseHeaders: hdrs }), null);
  assert.equal(answerSeen(s, null, { url: "https://wb.example/api/x", method: "GET", statusCode: 401, responseHeaders: hdrs }), null);
  assert.equal(s.size, 1);
});

test("a 404 for a kept URL drops that entry only", async () => {
  const s = createStore({ dir: tmp(), sealer: sealer() });
  await s.open();
  await put(s, "/api/object/X/1?tab=a", "{}", "d");
  await put(s, "/api/object/X/2", "{}", "d");
  const o = "https://wb.example";
  answerSeen(s, o, { url: `${o}/api/object/X/1?tab=a`, method: "GET", statusCode: 404, responseHeaders: { "x-plx-door": "d" } });
  assert.equal(await s.get("/api/object/X/1?tab=a"), null);
  assert.ok(await s.get("/api/object/X/2"));
});

test("a put is only of the newest door seen and a late put after a wipe lands nowhere", async () => {
  const dir = tmp();
  let release;
  let first = true;
  const slow = sealer({ seal: async (b) => { if (first) { first = false; await new Promise((r) => (release = r)); } return sealer().seal(b); } });
  const s = createStore({ dir, sealer: slow });
  await s.open();
  answer(s, "A");
  const pending = s.put({ key: "/api/a", body: "{}", door: "A", release: "r", fetchedAt: 1 }); // waits on the sealing
  await new Promise((r) => setImmediate(r));
  s.wipe("sign-out");   // the person signs out while the key is being sealed
  release();
  assert.equal(await pending, false);
  assert.deepEqual(walk(dir), []);
  // after the wipe nothing is kept until an answer is seen again; a late put of the old door is refused
  assert.equal(await s.put({ key: "/api/a", body: "{}", door: "A", release: "r", fetchedAt: 1 }), false);
  answer(s, "B");
  assert.equal(await s.put({ key: "/api/a", body: "{}", door: "A", release: "r", fetchedAt: 1 }), false);
  assert.equal(await s.put({ key: "/api/a", body: "{}", door: "B", release: "r", fetchedAt: 1 }), true);
});

test("entries older than seven days are dropped at open", async () => {
  const dir = tmp();
  let t = 1_000_000;
  const a = createStore({ dir, sealer: sealer(), now: () => t });
  await a.open();
  await put(a, "/api/old", "{}", "d", { fetchedAt: t });
  t += TTL_MS - 1000;
  await put(a, "/api/newer", "{}", "d", { fetchedAt: t });
  a.flush();
  t += 2000; // /api/old is now older than 7 days, /api/newer is not
  const b = createStore({ dir, sealer: sealer(), now: () => t });
  const o = await b.open();
  assert.deepEqual(o.keys, ["/api/newer"]);
  assert.equal(walk(path.join(dir, "e")).length, 1);
});

test("open names the keys used in the last hour for push catch-up", async () => {
  const dir = tmp();
  let t = 1_000_000;
  const a = createStore({ dir, sealer: sealer(), now: () => t });
  await a.open();
  await put(a, "/api/old", "{}", "d", { fetchedAt: t });
  t += 2 * 3600_000;
  await put(a, "/api/fresh", "{}", "d", { fetchedAt: t });
  a.flush();
  const b = createStore({ dir, sealer: sealer(), now: () => t });
  const o = await b.open();
  assert.deepEqual(o.keys.sort(), ["/api/fresh", "/api/old"]);
  assert.deepEqual(o.usedKeys, ["/api/fresh"]);
});

test("without sealing the durable tier is off and nothing reaches the disk", async () => {
  for (const off of [sealer({ available: async () => false }), sealer({ available: async () => { throw new Error("no keyring"); } }), null]) {
    const dir = tmp();
    const s = createStore({ dir, sealer: off });
    const o = await s.open();
    assert.equal(o.durable, false);
    assert.equal(await put(s, "/api/a", "{}", "d"), false);
    assert.deepEqual(walk(dir), []);
  }
});

test("a tampered or moved file is never returned, and an unreadable store opens as an empty one", async () => {
  const dir = tmp();
  const s = createStore({ dir, sealer: sealer() });
  await s.open();
  await put(s, "/api/a", '{"a":1}', "d");
  await put(s, "/api/b", '{"b":2}', "d");
  s.flush();
  const [fa, fb] = walk(path.join(dir, "e"));
  const A = fs.readFileSync(fa), B = fs.readFileSync(fb);
  fs.writeFileSync(fa, B); fs.writeFileSync(fb, A); // slots swapped
  assert.equal(await s.get("/api/a"), null);
  assert.equal(await s.get("/api/b"), null);
  // a bit flipped in the index: wiped at open
  const idx = path.join(dir, "index.bin");
  const raw = fs.readFileSync(idx); raw[raw.length - 1] ^= 1; fs.writeFileSync(idx, raw);
  const r = createStore({ dir, sealer: sealer() });
  assert.deepEqual((await r.open()).entries, []);
  assert.deepEqual(walk(dir), []);
});

test("a store sealed by another machine's key opens empty and is wiped", async () => {
  const dir = tmp();
  const s = createStore({ dir, sealer: sealer() });
  await s.open();
  await put(s, "/api/a", "{}", "d");
  s.flush();
  const other = sealer({ unseal: async () => { throw new Error("not this user's"); } });
  const r = createStore({ dir, sealer: other });
  assert.deepEqual((await r.open()).entries, []);
  assert.deepEqual(walk(dir), []);
});

test("the least recently used go first when the size cap is reached", async () => {
  const s = createStore({ dir: tmp(), sealer: sealer(), maxTotal: 6000 });
  await s.open();
  const big = JSON.stringify({ x: "y".repeat(1500) });
  for (const k of ["/api/1", "/api/2", "/api/3"]) await put(s, k, big, "d");
  await s.get("/api/1"); // used again: stays
  await put(s, "/api/4", big, "d");
  await put(s, "/api/5", big, "d");
  assert.ok(s.size < 5);
  assert.ok(await s.get("/api/5"));
});

test("keys that are not the Workbench's own reads are refused", async () => {
  const s = createStore({ dir: tmp(), sealer: sealer() });
  await s.open();
  for (const k of ["/api/ask", "/api/ask/stream", "/api/auth/dev-login", "/api/events", "/api/view-as/x", "/other", "https://evil.example/api/x", "/api/../etc", "", null, 5, "/api/" + "x".repeat(3000)]) {
    assert.equal(validKey(k), false, String(k));
    assert.equal(await put(s, k, "{}", "d"), false);
  }
  assert.equal(await put(s, "/api/a", "x".repeat(13 * 1024 * 1024), "d"), false); // over the per-answer cap
  assert.equal(s.size, 0);
});

const win = { webContents: {}, isDestroyed: () => false };
const frameOf = (url, parent = null) => ({ url, parent });
const ev = (url, parent) => ({ sender: win.webContents, senderFrame: frameOf(url, parent) });

test("every page call checks the sender in the main process: Microsoft's pages, other windows and sub-frames get nothing", async () => {
  const dir = tmp();
  const s = createStore({ dir, sealer: sealer() });
  await s.open();
  await put(s, "/api/a", '{"secret":1}', "d");
  const h = handlers({ store: s, getWin: () => win, getOrigin: () => "https://wb.example" });
  const ok = ev("https://wb.example/records");
  assert.equal((await h.get(ok, "/api/a")).body, '{"secret":1}');
  assert.equal((await h.open(ok)).entries.length, 1);
  const refused = [
    ev("https://login.microsoftonline.com/common/oauth2/authorize"),
    ev("https://wb.example.evil.example/"),
    ev("http://wb.example/"),
    ev("file:///tmp/x.html"),
    ev("https://wb.example/frame", frameOf("https://wb.example/")), // a sub-frame
    { sender: {}, senderFrame: frameOf("https://wb.example/") },     // another window
    { sender: win.webContents, senderFrame: null },
    {},
  ];
  for (const e of refused) {
    assert.equal(await h.get(e, "/api/a"), null);
    assert.deepEqual(await h.open(e), { durable: false, door: null, keys: [], entries: [] });
    assert.equal(await h.put(e, { key: "/api/z", body: "{}", door: "d" }), false);
    assert.equal(h.drop(e, "/api/a"), false);
    assert.equal(h.wipe(e), false);
  }
  assert.equal(s.size, 1);
  assert.equal(senderOk(ok, null, "https://wb.example"), false);
  assert.equal(senderOk(ok, win, null), false);
  assert.equal(h.wipe(ok), true);
  assert.equal(s.size, 0);
});

test("scale: a read costs the same with 1, 10, 60 or 5000 answers kept", async () => {
  const times = {};
  for (const n of [1, 10, 60, 5000]) {
    const s = createStore({ dir: tmp(), sealer: sealer(), maxTotal: 1 << 30 });
    await s.open();
    answer(s, "d");
    for (let i = 0; i < n; i++) assert.equal(await s.put({ key: `/api/k/${i}`, body: JSON.stringify({ i, pad: "z".repeat(2000) }), door: "d", release: "r", fetchedAt: Date.now() }), true);
    assert.equal(s.size, n); // none lost, none doubled
    const t0 = process.hrtime.bigint();
    for (let r = 0; r < 200; r++) assert.ok(await s.get(`/api/k/${r % n}`));
    times[n] = Number(process.hrtime.bigint() - t0) / 200 / 1e6;
    s.flush();
  }
  console.log("ms per get by stored answers:", JSON.stringify(times));
  assert.ok(times[5000] < Math.max(5, times[1] * 5), JSON.stringify(times));
});

test("the page loading again (a reload) gets what is held; it never wipes a store whose index is not yet written", async () => {
  const s = createStore({ dir: tmp(), sealer: sealer() });
  await s.open();
  await put(s, "/api/a", '{"a":1}', "d"); // the index file is written 1.5 s later
  const again = await s.open();
  assert.deepEqual(again.keys, ["/api/a"]);
  assert.equal(again.entries[0].body, '{"a":1}');
  assert.equal(s.size, 1);
});
