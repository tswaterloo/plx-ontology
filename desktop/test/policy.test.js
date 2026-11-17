"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const { normalizeServerUrl, allowInWindow, allowExternal, sanitizeNotification, extraSignInHosts, isSetupPage, looksLikeWorkbench } = require("../src/policy");

test("the server address is https, or http for this computer only, and nothing else", () => {
  assert.equal(normalizeServerUrl("https://workbench.example.com/"), "https://workbench.example.com");
  assert.equal(normalizeServerUrl("  https://wb.example.com:8443/x?y=1 "), "https://wb.example.com:8443");
  assert.equal(normalizeServerUrl("http://localhost:3000"), "http://localhost:3000");
  assert.equal(normalizeServerUrl("http://127.0.0.1:3000/"), "http://127.0.0.1:3000");
  for (const bad of ["http://workbench.example.com", "ftp://x.example", "file:///etc/passwd", "javascript:alert(1)", "https://u:p@x.example", "not a url", "", null, undefined, 5, "https://"]) {
    assert.equal(normalizeServerUrl(bad), null, String(bad));
  }
});

test("the window stays on the Workbench and Microsoft's sign-in", () => {
  const origin = "https://wb.example.com";
  assert.equal(allowInWindow("https://wb.example.com/late?x=1", origin), true);
  assert.equal(allowInWindow("https://login.microsoftonline.com/common/oauth2/v2.0/authorize", origin), true);
  for (const out of ["https://evil.example.com/", "https://wb.example.com.evil.example/", "http://wb.example.com/", "https://login.microsoftonline.com.evil.example/", "http://login.microsoftonline.com/", "file:///etc/passwd", "javascript:1", "data:text/html,x", "nonsense"]) {
    assert.equal(allowInWindow(out, origin), false, out);
  }
});

test("only web and mail links are handed to the person's own apps", () => {
  assert.equal(allowExternal("https://example.com/a"), true);
  assert.equal(allowExternal("mailto:a@b.example?subject=x"), true);
  for (const bad of ["http://example.com", "file:///C:/Windows/System32/calc.exe", "ms-msdt:/id", "smb://host/share", "javascript:1", "vscode://x", "nonsense"]) {
    assert.equal(allowExternal(bad), false, bad);
  }
});

test("a notification is short plain text with a link only inside the Workbench", () => {
  const origin = "https://wb.example.com";
  assert.deepEqual(sanitizeNotification({ title: "Late PO", body: "PO 100 is 5 days late", href: "/object/PurchaseOrder/100?x=1#a" }, origin), {
    title: "Late PO", body: "PO 100 is 5 days late", href: "/object/PurchaseOrder/100?x=1#a",
  });
  assert.equal(sanitizeNotification({ title: "x", href: "https://evil.example/phish" }, origin).href, null);
  assert.equal(sanitizeNotification({ title: "x", href: "//evil.example/phish" }, origin).href, null);
  assert.equal(sanitizeNotification({ title: "x", href: "javascript:alert(1)" }, origin).href, null);
  assert.equal(sanitizeNotification({ title: "x", href: "https://wb.example.com/late" }, origin).href, "/late");
  const long = sanitizeNotification({ title: "t".repeat(500) + "\n\u0000", body: "b".repeat(1000) }, origin);
  assert.equal(long.title.length, 120);
  assert.equal(long.body.length, 300);
  assert.doesNotMatch(sanitizeNotification({ title: "a\nb\u0007c" }, origin).title, /[\u0000-\u001f]/);
  for (const bad of [null, undefined, 5, "x", {}, { title: "" }, { title: "   " }, { title: 5 }]) assert.equal(sanitizeNotification(bad, origin), null);
});

test("a federated sign-in host is added only by exact name, and only over https", () => {
  const extra = extraSignInHosts(" sts.example.com, BAD host, *.wild.example, sts.example.com.evil.example ,,localhost");
  assert.deepEqual(extra, ["sts.example.com", "sts.example.com.evil.example"]);
  const origin = "https://wb.example.com";
  assert.equal(allowInWindow("https://sts.example.com/adfs/ls", origin, ["sts.example.com"]), true);
  assert.equal(allowInWindow("http://sts.example.com/adfs/ls", origin, ["sts.example.com"]), false);
  assert.equal(allowInWindow("https://x.sts.example.com/", origin, ["sts.example.com"]), false);
  assert.equal(allowInWindow("https://sts.example.com/", origin), false);
  assert.deepEqual(extraSignInHosts(undefined), []);
  // no server yet (first run): only sign-in pages, never an arbitrary origin
  assert.equal(allowInWindow("https://wb.example.com/", null), false);
});

test("the only local page the window may show is the first-run page itself", () => {
  const setup = "file:///opt/PLX%20Workbench/resources/app.asar/src/setup.html";
  assert.equal(isSetupPage(setup, setup), true);
  assert.equal(isSetupPage(`${setup}?x=1#y`, setup), true);
  for (const other of ["file:///etc/passwd", "file:///opt/PLX%20Workbench/resources/app.asar/src/other.html", "file://host/opt/PLX%20Workbench/resources/app.asar/src/setup.html", "https://wb.example.com/setup.html", "nonsense"]) {
    assert.equal(isSetupPage(other, setup), false, other);
  }
});

test("the first run accepts an address only when a Workbench answers its health check", () => {
  assert.equal(looksLikeWorkbench(200, { status: "ok", ops: {} }), true);
  for (const [status, body] of [[200, { status: "down" }], [200, null], [200, "ok"], [302, { status: "ok" }], [404, { status: "ok" }], [200, {}]]) {
    assert.equal(looksLikeWorkbench(status, body), false, JSON.stringify([status, body]));
  }
});

test("a page that failed to load is checked again soon, then less often, never more than every 30 s", () => {
  const { retryDelay } = require("../src/policy");
  assert.deepEqual([0, 1, 2, 3, 4, 5, 50].map(retryDelay), [2000, 4000, 8000, 16000, 30000, 30000, 30000]);
  for (const odd of [-1, 1.5, NaN, undefined, "3"]) assert.equal(retryDelay(odd), 2000);
});

test("the failure page says it retries on its own, and nothing it shows becomes markup", () => {
  const { failPage } = require("../src/policy");
  const page = failPage(`ERR<img src=x onerror=alert(1)>`, `https://wb.example/x"><script>alert(1)</script>`);
  assert.match(page, /opens the Workbench again as soon as it answers/);
  assert.doesNotMatch(page, /<img|<script/);
  assert.doesNotMatch(page, /onerror=alert\(1\)>/);
  assert.match(failPage("", "https://wb.example/"), /The page didn&#39;t load\./);
});

test("a retry or a reload stays on the Workbench's own origin, not a look-alike prefix", () => {
  const { sameOrigin } = require("../src/policy");
  assert.equal(sameOrigin("https://wb.example/today?x=1", "https://wb.example"), true);
  assert.equal(sameOrigin("https://wb.example.evil.test/", "https://wb.example"), false);
  assert.equal(sameOrigin("https://wb.example:8443/", "https://wb.example"), false);
  assert.equal(sameOrigin("not a url", "https://wb.example"), false);
});
