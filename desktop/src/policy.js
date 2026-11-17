"use strict";
// The rules the desktop shell applies, with no Electron in them so they can be tested:
// which address the window may show, which links leave for the browser, what a
// notification from the page may carry.

/** The Workbench address as the person typed it: https, or http only for this machine; no credentials, path or query. */
function normalizeServerUrl(input) {
  if (typeof input !== "string") return null;
  let u;
  try {
    u = new URL(input.trim());
  } catch {
    return null;
  }
  const local = u.hostname === "localhost" || u.hostname === "127.0.0.1" || u.hostname === "[::1]";
  if (u.protocol !== "https:" && !(u.protocol === "http:" && local)) return null;
  if (u.username || u.password || !u.hostname) return null;
  return u.origin;
}

/** Hosts the sign-in may pass through while the window is on the Workbench. */
const SIGN_IN_HOSTS = ["login.microsoftonline.com", "login.live.com", "login.microsoft.com"];

/** Extra sign-in hosts (a federated identity provider), from PLX_DESKTOP_SIGNIN_HOSTS: exact lowercase names, comma-separated. */
function extraSignInHosts(value) {
  if (typeof value !== "string") return [];
  return value
    .split(",")
    .map((h) => h.trim().toLowerCase())
    .filter((h) => /^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$/.test(h));
}

/** May the window itself go to `target`? The Workbench origin, and the sign-in pages (https only). */
function allowInWindow(target, serverOrigin, extraHosts = []) {
  let u;
  try {
    u = new URL(target);
  } catch {
    return false;
  }
  if (serverOrigin && u.origin === serverOrigin) return true;
  return u.protocol === "https:" && (SIGN_IN_HOSTS.includes(u.hostname) || extraHosts.includes(u.hostname));
}

/** The one local page the window may show: the first-run page, compared exactly (no other file). */
function isSetupPage(target, setupUrl) {
  try {
    const u = new URL(target);
    const s = new URL(setupUrl);
    return u.protocol === "file:" && u.pathname === s.pathname && u.host === s.host;
  } catch {
    return false;
  }
}

/** Did /api/health answer like a Workbench? (a JSON body whose status is "ok") */
function looksLikeWorkbench(status, body) {
  if (status !== 200 || !body || typeof body !== "object") return false;
  return body.status === "ok";
}

/** May a link that wants a new window be handed to the person's own browser or mail client? */
function allowExternal(target) {
  let u;
  try {
    u = new URL(target);
  } catch {
    return false;
  }
  return u.protocol === "https:" || u.protocol === "mailto:";
}

/** A notification request from the page, made safe: plain short text, and a link only inside the Workbench. */
function sanitizeNotification(n, serverOrigin) {
  if (!n || typeof n !== "object") return null;
  const clean = (v, max) => (typeof v === "string" ? v.replace(/[\u0000-\u001f\u007f]+/g, " ").trim().slice(0, max) : "");
  const title = clean(n.title, 120);
  if (!title) return null;
  const body = clean(n.body, 300);
  let href = null;
  if (typeof n.href === "string") {
    try {
      const u = new URL(n.href, serverOrigin);
      if (u.origin === serverOrigin) href = u.pathname + u.search + u.hash;
    } catch {
      href = null;
    }
  }
  return { title, body, href };
}

/** The in-app destinations the menu may send the page to. */
const MENU_ACTIONS = new Set(["feedback", "home", "ask", "my-work", "autopilot"]);

/** How long the window waits before checking the Workbench again after a page failed to
 * load (an API restart, a dropped VPN): 2 s, doubling, at most 30 s. */
function retryDelay(attempt) {
  const n = Number.isInteger(attempt) && attempt > 0 ? attempt : 0;
  return Math.min(30_000, 2_000 * 2 ** Math.min(n, 4));
}

/** The page shown when the Workbench could not be loaded: what failed, in plain words,
 * that it is tried again on its own, and a button to try now. Nothing of ``desc`` or
 * ``url`` can become markup or script. */
function failPage(desc, url) {
  const esc = (t) => t.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  const said = esc(String(desc || "The page didn't load"));
  const target = esc(JSON.stringify(String(url))); // a JS string, inside a quoted attribute
  return (
    `<body style="font:14px system-ui;margin:3rem;max-width:34rem"><h1 style="font-size:1.2rem">Can't reach the Workbench</h1>` +
    `<p>${said}. Check your connection (and the VPN, if the Workbench is on one).</p>` +
    `<p>The window opens the Workbench again as soon as it answers.</p>` +
    `<p><button onclick="location.href=${target}">Try now</button></p></body>`
  );
}

/** A crashed page is reloaded at most this many times within CRASH_WINDOW_MS; past it the
 * window says the page keeps stopping instead of reloading in a loop. */
const CRASH_RELOADS = 3;
const CRASH_WINDOW_MS = 5 * 60_000;

/** Whether ``target`` is on the Workbench's own origin (scheme, host and port), not just a
 * text prefix of it. */
function sameOrigin(target, serverOrigin) {
  try {
    return new URL(target).origin === new URL(serverOrigin).origin;
  } catch {
    return false;
  }
}

module.exports = { retryDelay, failPage, sameOrigin, CRASH_RELOADS, CRASH_WINDOW_MS, normalizeServerUrl, allowInWindow, extraSignInHosts, isSetupPage, looksLikeWorkbench, allowExternal, sanitizeNotification, MENU_ACTIONS, SIGN_IN_HOSTS };
