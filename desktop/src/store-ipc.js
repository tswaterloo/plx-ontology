"use strict";
// The page store's two doors, kept apart from Electron so they can be tested:
//  - `answerSeen`: what the one webRequest listener does with each API answer's status and
//    headers, BEFORE the body is delivered to the page (wipe rules, 404 drop);
//  - `handlers`: the page's calls (open / get / put / drop / wipe), each refusing a sender that
//    is not the Workbench's own page in the app's window. The preload also runs on Microsoft's
//    sign-in pages, so the sender's origin is checked here, in the main process, every time.

const { validKey } = require("./store");

const header = (headers, name) => {
  if (!headers || typeof headers !== "object") return null;
  const want = name.toLowerCase();
  for (const k of Object.keys(headers)) {
    if (k.toLowerCase() === want) {
      const v = headers[k];
      return Array.isArray(v) ? String(v[0] ?? "") : String(v ?? "");
    }
  }
  return null;
};

/** Apply one API answer to `store`. Returns "wiped" when the page must forget its own copy
 * too (it is told), "kept" otherwise, null for a request that is not the Workbench's API. */
function answerSeen(store, serverOrigin, details) {
  if (!serverOrigin || !details || typeof details.url !== "string") return null;
  let u;
  try {
    u = new URL(details.url);
  } catch {
    return null;
  }
  if (u.origin !== serverOrigin || !u.pathname.startsWith("/api/")) return null;
  const method = String(details.method || "GET").toUpperCase();
  if (method === "POST" && u.pathname === "/api/auth/logout") {
    store.wipe("sign-out");
    return "wiped"; // a sign-out that failed still forgets: the person asked to leave
  }
  const out = store.observe({
    refused: header(details.responseHeaders, "x-plx-session") === "refused",
    door: header(details.responseHeaders, "x-plx-door"),
  });
  if (method === "GET" && details.statusCode === 404) store.drop(u.pathname + u.search);
  return out;
}

/** True for the Workbench's own page, in the main frame of the app's window. */
function senderOk(e, win, serverOrigin) {
  try {
    if (!win || (win.isDestroyed && win.isDestroyed()) || !serverOrigin) return false;
    if (!e || e.sender !== win.webContents) return false;
    const frame = e.senderFrame;
    if (!frame || frame.parent) return false;
    return new URL(frame.url).origin === serverOrigin;
  } catch {
    return false;
  }
}

/** The page's calls. `ctx`: { store, getWin(), getOrigin(), release(): string }. A refused
 * sender gets the plain "not available" a missing store gives. */
function handlers(ctx) {
  const guard = (e) => senderOk(e, ctx.getWin(), ctx.getOrigin());
  const none = { durable: false, door: null, keys: [], entries: [] };
  return {
    open: async (e) => (guard(e) ? ctx.store.open() : none),
    get: async (e, key) => (guard(e) && validKey(key) ? ctx.store.get(key) : null),
    put: async (e, a) => {
      if (!guard(e) || !a || typeof a !== "object") return false;
      return ctx.store.put({ key: a.key, body: a.body, door: a.door, release: a.release, fetchedAt: a.fetchedAt });
    },
    drop: (e, key) => (guard(e) ? ctx.store.drop(key) : false),
    wipe: (e) => {
      if (!guard(e)) return false;
      ctx.store.wipe("page asked");
      return true;
    },
  };
}

module.exports = { answerSeen, handlers, senderOk, header };
