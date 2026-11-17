"use strict";
// Starts the real shell against a stub server and checks what the page can and cannot reach.
// Needs Electron installed and a display (xvfb-run on a Linux server); run with PLX_DESKTOP_SMOKE=1,
// and PLX_DESKTOP_BINARY=<packaged executable> to check a built app instead of the sources.
const test = require("node:test");
const assert = require("node:assert/strict");
const http = require("node:http");
const { spawn } = require("node:child_process");
const path = require("node:path");

const PAGE = `<!doctype html><title>stub</title><script>
  fetch("/probe", { method: "POST", body: JSON.stringify({
    app: window.plxDesktop && window.plxDesktop.app,
    keys: Object.keys(window.plxDesktop || {}).sort(),
    hasRequire: typeof require, hasProcess: typeof process, title: document.title }) })
    .then(() => { location.href = "/redir"; });
</script>`;

test("the window loads the Workbench page with the bridge and nothing else, and does not follow a redirect off it", { skip: process.env.PLX_DESKTOP_SMOKE !== "1", timeout: 60000 }, async () => {
  let child;
  let server;
  let outside;
  const outsideHits = [];
  try {
    // another origin the Workbench page tries to reach by a server redirect: the window must not follow
    outside = http.createServer((req, res) => {
      outsideHits.push(req.url);
      res.end("outside");
    });
    await new Promise((r) => outside.listen(0, "127.0.0.1", r));
    let redirected;
    const redirectSeen = new Promise((r) => (redirected = r));
    const report = await new Promise((resolve, reject) => {
      server = http.createServer((req, res) => {
        if (req.url === "/redir") {
          res.writeHead(302, { Location: `http://127.0.0.1:${outside.address().port}/stolen` });
          res.end();
          redirected();
          return;
        }
        if (req.method !== "POST") {
          res.setHeader("Content-Type", "text/html");
          res.end(PAGE);
          return;
        }
        let body = "";
        req.on("data", (c) => (body += c));
        req.on("end", () => {
          res.end("ok");
          resolve(JSON.parse(body));
        });
      });
      server.listen(0, "127.0.0.1", () => {
        // PLX_DESKTOP_BINARY: a packaged build's executable (then its updater runs too, against the build's feed)
        const packaged = process.env.PLX_DESKTOP_BINARY;
        const cmd = packaged ? [packaged, "--no-sandbox", "--disable-gpu"] : [require("electron"), "--no-sandbox", "--disable-gpu", path.join(__dirname, "..")];
        const env = { ...process.env, PLX_WORKBENCH_URL: `http://127.0.0.1:${server.address().port}` };
        if (!packaged) env.PLX_DESKTOP_NO_UPDATE = "1";
        child = spawn(process.platform === "linux" ? "xvfb-run" : cmd.shift(), process.platform === "linux" ? ["-a", ...cmd] : cmd, {
          env,
          stdio: "ignore",
          detached: true,
        });
        child.on("error", reject);
        setTimeout(() => reject(new Error("the page never reported")), 45000).unref();
      });
    });
    assert.equal(report.app, true);
    assert.deepEqual(report.keys, ["app", "notify", "onMenu", "saveServer", "version"]);
    assert.equal(report.hasRequire, "undefined"); // no Node in the page
    assert.equal(report.hasProcess, "undefined");
    assert.equal(report.title, "stub");
    await redirectSeen;
    await new Promise((r) => setTimeout(r, 3000));
    assert.deepEqual(outsideHits, []); // the redirect off the Workbench was stopped
  } finally {
    if (child) try { process.kill(-child.pid, "SIGKILL"); } catch { /* already gone */ }
    if (server) server.close();
    if (outside) outside.close();
  }
});
