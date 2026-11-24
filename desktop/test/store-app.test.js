"use strict";
// The page store inside the real shell, driven by Playwright's Electron driver against a stub
// Workbench (SPEED-SPECS 10.9, check H1): the one webRequest listener wipes on a moved door, a
// refused session, a 404; the page's calls are refused from any page but the Workbench's own;
// closing hides; the store survives a relaunch. Needs Electron and playwright-core:
//   PLX_DESKTOP_SMOKE=1 PLX_ELECTRON=<electron binary> node --test test/store-app.test.js
// (on a server run under Xvfb, DISPLAY set; PLX_DESKTOP_TEST_SEAL=1 lets a
// development run seal on a Linux box without a keyring).
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const os = require("node:os");
const path = require("node:path");

const SKIP = process.env.PLX_DESKTOP_SMOKE !== "1" || !process.env.PLX_ELECTRON;

function stub() {
  const state = { door: "D1" };
  const server = http.createServer((req, res) => {
    const u = new URL(req.url, "http://x");
    const h = { "Content-Type": "application/json", "X-PLX-Door": state.door };
    if (u.pathname === "/") {
      res.setHeader("Content-Type", "text/html");
      return res.end("<!doctype html><title>stub</title><p>hi</p>");
    }
    if (u.pathname === "/api/health") return res.writeHead(200, { "Content-Type": "application/json" }).end('{"status":"ok"}');
    if (u.pathname === "/api/refuse") return res.writeHead(401, { "Content-Type": "application/json", "X-PLX-Session": "refused" }).end("{}");
    if (u.pathname === "/api/route403") return res.writeHead(403, h).end("{}"); // an ordinary "not for you"
    if (u.pathname === "/api/gone") return res.writeHead(404, h).end("{}");
    if (u.pathname === "/api/auth/logout" && req.method === "POST") return res.writeHead(200, h).end("{}");
    return res.writeHead(200, h).end(JSON.stringify({ path: u.pathname + u.search, door: state.door }));
  });
  return { state, server, start: () => new Promise((r) => server.listen(0, "127.0.0.1", () => r(server.address().port))) };
}

async function launch(userData, port) {
  const { _electron } = require(process.env.PW_CORE || "playwright-core");
  const app = await _electron.launch({
    executablePath: process.env.PLX_ELECTRON,
    args: [path.join(__dirname, ".."), "--no-sandbox", "--disable-gpu", `--user-data-dir=${userData}`],
    env: { ...process.env, PLX_WORKBENCH_URL: `http://127.0.0.1:${port}`, PLX_DESKTOP_NO_UPDATE: "1", PLX_DESKTOP_TEST_SEAL: "1" },
  });
  const page = await app.firstWindow();
  await page.waitForLoadState("domcontentloaded");
  return { app, page };
}

test("the store inside the shell: keeps, wipes by door, refusal, sign-out and 404; refuses other pages; survives a relaunch; close hides", { skip: SKIP, timeout: 180000 }, async () => {
  const s = stub();
  const port = await s.start();
  const userData = fs.mkdtempSync(path.join(os.tmpdir(), "plx-app-"));
  let a;
  try {
    a = await launch(userData, port);
    let { app, page } = a;
    await page.evaluate(() => { window.__wiped = 0; window.plxDesktop.store.onWiped(() => { window.__wiped += 1; }); });
    const api = (p, init) => page.evaluate(([u, i]) => fetch(u, i).then((r) => r.status), [p, init]);
    const st = (fn, ...args) => page.evaluate(([f, a2]) => window.plxDesktop.store[f](...a2), [fn, args]);
    const entry = (key, door) => ({ key, body: JSON.stringify({ k: key }), door, release: "r1", fetchedAt: Date.now() });

    // the bridge shape, and the store opens sealed
    assert.deepEqual(await page.evaluate(() => Object.keys(window.plxDesktop.store).sort()), ["drop", "get", "onWiped", "open", "put", "wipe"]);
    const opened = await st("open");
    assert.equal(opened.durable, true);

    // an answer is kept under the door the listener saw, and a put of any other door is refused
    assert.equal(await api("/api/a"), 200);
    assert.equal(await st("put", entry("/api/a", "D1")), true);
    assert.equal(await st("put", entry("/api/b", "OTHER")), false);
    assert.equal((await st("get", "/api/a")).body, JSON.stringify({ k: "/api/a" }));

    // an ordinary 403 (a route's own "not for you") and the same door change nothing
    assert.equal(await api("/api/route403"), 403);
    assert.equal(await api("/api/a"), 200);
    assert.notEqual(await st("get", "/api/a"), null);

    // a 404 drops that entry only
    await api("/api/b");
    assert.equal(await st("put", entry("/api/gone", "D1")), true);
    await api("/api/gone");
    assert.equal(await st("get", "/api/gone"), null);
    assert.notEqual(await st("get", "/api/a"), null);

    // a moved door empties the store before the page can use the answer, and the page is told
    s.state.door = "D2";
    await api("/api/a");
    assert.equal(await st("get", "/api/a"), null);
    await page.waitForFunction(() => window.__wiped >= 1);
    assert.equal(await st("put", entry("/api/a", "D1")), false); // the old person's late write lands nowhere
    assert.equal(await st("put", entry("/api/a", "D2")), true);

    // a refused session (credentials sent and refused) wipes; sign-out wipes even when it failed
    await api("/api/refuse");
    assert.equal(await st("get", "/api/a"), null);
    await api("/api/a");
    assert.equal(await st("put", entry("/api/a", "D2")), true);
    await api("/api/auth/logout", { method: "POST" });
    assert.equal(await st("get", "/api/a"), null);

    // the files on disk hold no plaintext
    await api("/api/a");
    await st("put", { ...entry("/api/secret-record-8812", "D2"), body: "TOPSECRETBODY" });
    await new Promise((r) => setTimeout(r, 2000)); // the index is written 1.5 s after a change
    const files = fs.readdirSync(path.join(userData, "store"), { recursive: true }).map((f) => path.join(userData, "store", f)).filter((f) => fs.statSync(f).isFile());
    assert.ok(files.length >= 3);
    for (const f of files) {
      const raw = fs.readFileSync(f);
      assert.equal(raw.includes("TOPSECRETBODY"), false, f);
      assert.equal(f.includes("8812"), false, f);
    }

    // another page in the window (the first-run page: a file, as Microsoft's sign-in page would be) gets nothing
    await app.evaluate(({ BrowserWindow }, p) => BrowserWindow.getAllWindows()[0].loadURL("file://" + p), path.join(__dirname, "..", "src", "setup.html"));
    await page.waitForFunction(() => location.protocol === "file:");
    assert.deepEqual(await st("open"), { durable: false, door: null, keys: [], entries: [] });
    assert.equal(await st("put", entry("/api/z", "D2")), false);
    assert.equal(await st("get", "/api/a"), null);
    assert.equal(await st("wipe"), false);
    await app.evaluate(({ BrowserWindow }, u) => BrowserWindow.getAllWindows()[0].loadURL(u), `http://127.0.0.1:${port}/`);
    await page.waitForFunction(() => location.protocol === "http:");

    // closing hides; the window and its page live on
    const shown = await app.evaluate(({ BrowserWindow }) => { const w = BrowserWindow.getAllWindows()[0]; w.show(); w.close(); return [w.isDestroyed(), w.isVisible()]; });
    assert.deepEqual(shown, [false, false]);
    assert.equal(await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows().length), 1);

    // survives a relaunch: kept, then read back on open() with the door
    assert.equal(await api("/api/a"), 200);
    assert.equal(await st("put", entry("/api/a", "D2")), true);
    await new Promise((r) => setTimeout(r, 2000)); // the index is written 1.5 s after a change
    await app.evaluate(({ app: electronApp }) => electronApp.quit()).catch(() => undefined);
    await app.close().catch(() => undefined);
    a = null;
    const t0 = Date.now();
    const b = await launch(userData, port);
    a = b;
    const again = await b.page.evaluate(() => window.plxDesktop.store.open());
    console.log(`relaunch: window up and store opened in ${Date.now() - t0} ms (Linux headless, not a laptop)`);
    assert.equal(again.durable, true);
    assert.equal(again.door, "D2");
    assert.deepEqual([...again.keys].sort(), ["/api/a", "/api/secret-record-8812"]);
    // a different person on the same machine: their first answer empties it before use
    s.state.door = "D3";
    await b.page.evaluate(() => fetch("/api/a").then((r) => r.status));
    assert.deepEqual((await b.page.evaluate(() => window.plxDesktop.store.get("/api/a"))), null);
  } finally {
    if (a) await a.app.close().catch(() => undefined);
    s.server.close();
  }
});
