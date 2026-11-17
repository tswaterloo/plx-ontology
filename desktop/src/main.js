"use strict";
// PLX Workbench desktop shell: one window onto the same Workbench the browser shows (same
// sign-in, same pages, same permissions: the server decides, nothing is cached or answered
// here). It adds a window and icon, a menu, system notifications the page asks for, an
// in-app "Give feedback", and updates from our own server.

const path = require("node:path");
const fs = require("node:fs");
const { pathToFileURL } = require("node:url");
const { app, BrowserWindow, Menu, Notification, dialog, ipcMain, net, session, shell } = require("electron");
const policy = require("./policy");

let win = null;
let serverOrigin = null;
const SETUP_PAGE = path.join(__dirname, "setup.html");
const SETUP_URL = pathToFileURL(SETUP_PAGE).href;
const signInHosts = policy.extraSignInHosts(process.env.PLX_DESKTOP_SIGNIN_HOSTS || "");

const configFile = () => path.join(app.getPath("userData"), "config.json");

function readServer() {
  const fromEnv = policy.normalizeServerUrl(process.env.PLX_WORKBENCH_URL || "");
  if (fromEnv) return fromEnv;
  try {
    return policy.normalizeServerUrl(JSON.parse(fs.readFileSync(configFile(), "utf8")).serverUrl);
  } catch {
    return null;
  }
}

function saveServer(origin) {
  fs.mkdirSync(path.dirname(configFile()), { recursive: true });
  fs.writeFileSync(configFile(), JSON.stringify({ serverUrl: origin }), { mode: 0o600 });
}

const send = (msg) => win && !win.isDestroyed() && win.webContents.send("desktop:menu", msg);

function show() {
  if (!win) return;
  if (win.isMinimized()) win.restore();
  win.show();
  win.focus();
}

// The pending check of a Workbench that failed to load (did-fail-load), at most one.
let retry = null;
// The load the last health check reopened ({ url, attempt }), until a page finishes loading.
let retried = null;
// When the page's process last crashed (render-process-gone), within policy.CRASH_WINDOW_MS.
let crashes = [];

function stopRetry() {
  if (retry) clearTimeout(retry);
  retry = null;
}

/** Check the server's health after ``policy.retryDelay(attempt)``; when it answers like a
 * Workbench, load ``url`` again, else check again later. Stops when the window closes,
 * the server is changed, or another page loads. */
function retryWhenBack(url, attempt) {
  stopRetry();
  const origin = serverOrigin;
  retry = setTimeout(async () => {
    retry = null;
    if (!win || win.isDestroyed() || serverOrigin !== origin) return;
    let ok = false;
    try {
      const res = await net.fetch(`${origin}/api/health`, { redirect: "error", cache: "no-store", signal: AbortSignal.timeout(10_000) });
      ok = policy.looksLikeWorkbench(res.status, await res.json().catch(() => null));
    } catch {
      ok = false;
    }
    if (!win || win.isDestroyed() || serverOrigin !== origin) return;
    if (ok) {
      retried = { url, attempt };
      win.loadURL(url);
    } else retryWhenBack(url, attempt + 1);
  }, policy.retryDelay(attempt));
}

function openStart() {
  stopRetry();
  if (!win) return;
  if (serverOrigin) win.loadURL(`${serverOrigin}/`);
  else win.loadFile(SETUP_PAGE);
}

function createWindow() {
  win = new BrowserWindow({
    width: 1280,
    height: 860,
    minWidth: 900,
    minHeight: 600,
    show: false,
    title: "PLX Workbench",
    icon: path.join(__dirname, "..", "build", "icon.png"),
    backgroundColor: "#ffffff",
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      sandbox: true,
      nodeIntegration: false,
      webSecurity: true,
      spellcheck: true,
    },
  });
  win.once("ready-to-show", () => win.show());
  win.on("closed", () => {
    stopRetry();
    win = null;
  });
  // the window never leaves the Workbench (or Microsoft's sign-in); anything else opens in the browser
  // (a server redirect is checked like a click: will-redirect)
  const guard = (e, url) => {
    if (policy.isSetupPage(url, SETUP_URL)) return; // the first-run page, and no other file
    if (!policy.allowInWindow(url, serverOrigin, signInHosts)) {
      e.preventDefault();
      if (policy.allowExternal(url)) shell.openExternal(url);
    }
  };
  win.webContents.on("will-navigate", guard);
  win.webContents.on("will-redirect", (e, url, _inPlace, isMainFrame) => {
    if (isMainFrame) guard(e, url);
  });
  win.webContents.on("will-attach-webview", (e) => e.preventDefault());
  win.webContents.setWindowOpenHandler(({ url }) => {
    if (policy.allowExternal(url)) shell.openExternal(url);
    return { action: "deny" };
  });
  // a load that failed says so in the window instead of leaving it blank, and the window
  // opens the page again by itself once the Workbench answers (a restart for an update
  // takes it away for a minute; nobody should have to notice and click)
  win.webContents.on("did-fail-load", (_e, code, desc, url, isMainFrame) => {
    if (!isMainFrame || code === -3) return; // -3: aborted by a newer navigation
    if (String(url).startsWith("data:")) return; // the failure page itself
    win.loadURL(`data:text/html;charset=utf-8,${encodeURIComponent(policy.failPage(desc, url))}`);
    // a page that failed again right after its health check passed waits longer each time
    const attempt = retried && retried.url === url ? retried.attempt + 1 : 0;
    retried = null;
    if (serverOrigin && policy.sameOrigin(url, serverOrigin)) retryWhenBack(url, attempt);
  });
  win.webContents.on("did-navigate", (_e, url) => {
    if (!String(url).startsWith("data:")) stopRetry(); // a page loaded: nothing to retry
  });
  win.webContents.on("did-finish-load", () => {
    const url = win && !win.isDestroyed() ? win.webContents.getURL() : "";
    if (!url.startsWith("data:")) retried = null;
  });
  // the page's process crashed or was killed (out of memory): load it again rather than
  // leave a blank window
  win.webContents.on("render-process-gone", (_e, details) => {
    if (!win || win.isDestroyed() || details.reason === "clean-exit") return;
    const url = win.webContents.getURL();
    const now = Date.now();
    crashes = crashes.filter((t) => now - t < policy.CRASH_WINDOW_MS).concat(now);
    if (crashes.length > policy.CRASH_RELOADS) {
      // the page keeps crashing: say so (and keep checking) instead of a reload loop
      win.loadURL(`data:text/html;charset=utf-8,${encodeURIComponent(policy.failPage("The Workbench page stopped several times in a row", url))}`);
      if (serverOrigin && policy.sameOrigin(url, serverOrigin)) retryWhenBack(url, 4); // every 30 s
      return;
    }
    if (serverOrigin && policy.sameOrigin(url, serverOrigin)) win.loadURL(url);
    else openStart();
  });
  openStart();
}

function buildMenu() {
  const go = (action) => () => send({ action });
  const template = [
    {
      label: "File",
      submenu: [
        { label: "Change server…", click: () => { serverOrigin = null; openStart(); } },
        { type: "separator" },
        process.platform === "darwin" ? { role: "close" } : { role: "quit" },
      ],
    },
    { role: "editMenu" },
    {
      label: "Go",
      submenu: [
        { label: "Today", accelerator: "CmdOrCtrl+1", click: go("home") },
        { label: "Ask", accelerator: "CmdOrCtrl+2", click: go("ask") },
        { label: "My work", accelerator: "CmdOrCtrl+3", click: go("my-work") },
        { label: "Autopilot", accelerator: "CmdOrCtrl+4", click: go("autopilot") },
      ],
    },
    {
      label: "View",
      submenu: [
        { role: "reload" },
        { role: "resetZoom" },
        { role: "zoomIn" },
        { role: "zoomOut" },
        { type: "separator" },
        { role: "togglefullscreen" },
        ...(app.isPackaged ? [] : [{ role: "toggleDevTools" }]),
      ],
    },
    {
      label: "Help",
      submenu: [
        { label: "Give feedback…", click: go("feedback") },
        { label: "Check for updates…", click: () => void checkForUpdates(true) },
        { type: "separator" },
        { label: `Version ${app.getVersion()}`, enabled: false },
      ],
    },
  ];
  if (process.platform === "darwin") template.unshift({ role: "appMenu" });
  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}

// ---- updates: from our own server's /desktop/ folder (electron-builder's generic feed)
let updater = null;
function getUpdater() {
  if (updater) return updater;
  const { autoUpdater } = require("electron-updater");
  autoUpdater.autoDownload = true;
  autoUpdater.autoInstallOnAppQuit = true;
  autoUpdater.on("update-downloaded", () => {
    if (!Notification.isSupported()) return;
    new Notification({ title: "Workbench update ready", body: "It installs when you quit and reopen the app." }).show();
  });
  autoUpdater.on("error", () => undefined); // a missing feed is not the person's problem
  updater = autoUpdater;
  return updater;
}

async function checkForUpdates(manual) {
  const tell = (message) => manual && win && dialog.showMessageBox(win, { type: "info", title: "Updates", message });
  if (!app.isPackaged) return tell("Updates are checked only in an installed app.");
  if (process.env.PLX_DESKTOP_NO_UPDATE === "1") return tell("Update checks are turned off (PLX_DESKTOP_NO_UPDATE).");
  let result = null;
  try {
    result = await getUpdater().checkForUpdates();
  } catch {
    return tell("Couldn't reach the update feed. The app keeps working; try again later.");
  }
  const next = result && result.updateInfo && result.updateInfo.version;
  if (next && next !== app.getVersion() && result.isUpdateAvailable !== false) {
    return tell(`Version ${next} is downloading; it installs when you quit and reopen the app.`);
  }
  return tell(`You have the latest version (${app.getVersion()}).`);
}

// ---- the page's two requests
let recent = [];
ipcMain.on("desktop:version", (e) => {
  e.returnValue = app.getVersion();
});
ipcMain.on("desktop:notify", (e, req) => {
  if (!win || e.sender !== win.webContents || !serverOrigin) return;
  let from = null;
  try {
    from = new URL(e.senderFrame.url).origin;
  } catch {
    return;
  }
  if (from !== serverOrigin) return; // only the Workbench's own pages
  const n = policy.sanitizeNotification(req, serverOrigin);
  if (!n || !Notification.isSupported()) return;
  const now = Date.now();
  recent = recent.filter((t) => now - t < 60_000);
  if (recent.length >= 5) return; // a runaway page cannot flood the desktop
  recent.push(now);
  const note = new Notification({ title: n.title, body: n.body, silent: false });
  note.on("click", () => {
    show();
    if (n.href) send({ href: n.href });
  });
  note.show();
});
ipcMain.handle("desktop:save-server", async (e, url) => {
  // only the first-run page (a local file) may set the address
  if (!win || e.sender !== win.webContents || !policy.isSetupPage(e.senderFrame.url, SETUP_URL)) return { error: "Not allowed." };
  const origin = policy.normalizeServerUrl(url);
  if (!origin) return { error: "Enter a full https:// address (http:// only for this computer)." };
  // is a Workbench there? (its public health check; no sign-in needed, nothing sent but the request)
  let ok = false;
  try {
    const res = await net.fetch(`${origin}/api/health`, { redirect: "error", signal: AbortSignal.timeout(10_000) });
    ok = policy.looksLikeWorkbench(res.status, await res.json().catch(() => null));
  } catch {
    ok = false;
  }
  if (!ok) return { error: `No Workbench answered at ${origin}. Check the address (and the VPN, if it is on one).` };
  saveServer(origin);
  serverOrigin = origin;
  openStart();
  return { ok: true };
});

if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on("second-instance", show);
  app.whenReady().then(() => {
    // no permission is granted to a page; notifications go through the one IPC door above
    session.defaultSession.setPermissionRequestHandler((_wc, _perm, cb) => cb(false));
    session.defaultSession.setPermissionCheckHandler(() => false);
    serverOrigin = readServer();
    buildMenu();
    createWindow();
    void checkForUpdates(false);
    app.on("activate", () => {
      if (!win) createWindow();
      else show();
    });
  });
  app.on("window-all-closed", () => {
    if (process.platform !== "darwin") app.quit();
  });
}
