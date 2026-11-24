"use strict";
// PLX Workbench desktop shell: one window onto the same Workbench the browser shows (same
// sign-in, same pages, same permissions: the server decides, nothing is cached or answered
// here). It adds a window and icon, a menu, system notifications the page asks for, an
// in-app "Give feedback", and updates from our own server.
//
// It also keeps the answers the server sent THIS person on this machine (store.js: one
// encrypted file per answer), so the first screen after a launch draws at once. The page
// stays the only thing that asks the server; this process only keeps what the page hands it,
// and forgets it all when the sign-in, the server or the person's door changes (see
// `watchAnswers`). The window is hidden, not closed, so opening the app again is a show.

const path = require("node:path");
const fs = require("node:fs");
const { pathToFileURL } = require("node:url");
const { app, BrowserWindow, Menu, Notification, Tray, dialog, ipcMain, nativeImage, net, safeStorage, session, shell } = require("electron");
const policy = require("./policy");
const { createStore } = require("./store");
const storeIpc = require("./store-ipc");

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

function readConfig() {
  try {
    const c = JSON.parse(fs.readFileSync(configFile(), "utf8"));
    return c && typeof c === "object" ? c : {};
  } catch {
    return {};
  }
}

function writeConfig(patch) {
  fs.mkdirSync(path.dirname(configFile()), { recursive: true });
  fs.writeFileSync(configFile(), JSON.stringify({ ...readConfig(), ...patch }), { mode: 0o600 });
}

function saveServer(origin) {
  writeConfig({ serverUrl: origin });
}

// ---- the page store (durable tier): sealed by the OS, one file per answer
const sealer = {
  async available() {
    try {
      // Linux without a keyring falls back to a fixed password ("basic_text"): not encryption
      // (a development run on a Linux box without a keyring may set PLX_DESKTOP_TEST_SEAL=1 to exercise the store;
      // an installed app never honours it)
      const testing = process.env.PLX_DESKTOP_TEST_SEAL === "1" && !app.isPackaged;
      if (!testing && typeof safeStorage.getSelectedStorageBackend === "function" && safeStorage.getSelectedStorageBackend() === "basic_text") return false;
      return await safeStorage.isAsyncEncryptionAvailable();
    } catch {
      return false;
    }
  },
  async seal(buf) {
    return safeStorage.encryptStringAsync(buf.toString("base64"));
  },
  async unseal(sealed) {
    const got = await safeStorage.decryptStringAsync(sealed);
    return { key: Buffer.from(got.result, "base64"), reseal: !!got.shouldReEncrypt };
  },
};
let store = null;
const getStore = () => {
  if (!store) store = createStore({ dir: path.join(app.getPath("userData"), "store"), sealer, log: (m) => console.log(m) });
  return store;
};

/** The ONE listener on the Workbench's API answers (Electron keeps only the last listener per
 * session): it sees status and headers before the body reaches the page, so a changed door or a
 * refused session empties the store before any stored answer can be drawn for the new state. */
function watchAnswers() {
  const webRequest = session.defaultSession.webRequest;
  if (!serverOrigin) {
    webRequest.onHeadersReceived(null);
    return;
  }
  webRequest.onHeadersReceived({ urls: [`${serverOrigin}/api/*`] }, (details, callback) => {
    try {
      if (storeIpc.answerSeen(getStore(), serverOrigin, details) === "wiped" && win && !win.isDestroyed()) win.webContents.send("desktop:wiped");
    } catch (e) {
      console.log(`store: an answer was not checked (${e && e.message ? String(e.message).slice(0, 60) : "error"}); wiped`);
      getStore().wipe("answer check failed"); // never keep what could not be checked
    }
    callback({});
  });
}

/** The server changed (or was first set): nothing kept for another server is ever shown. */
function serverChanged(next) {
  if (next !== serverOrigin) getStore().wipe("server changed");
  serverOrigin = next;
  watchAnswers();
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

let quitting = false;
let shownOnce = false;
let hiddenSince = null; // when the window was hidden (an update installs after it was hidden a while)
let startHidden = false; // opened at login, or restarted by an update: no window until the person asks
const relaunchMarker = () => path.join(app.getPath("userData"), "relaunch-hidden");
function consumeRelaunchMarker() {
  try {
    const was = fs.existsSync(relaunchMarker());
    if (was) fs.rmSync(relaunchMarker(), { force: true });
    return was;
  } catch {
    return false;
  }
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
      backgroundThrottling: false, // a hidden window keeps its page live: reopening is a show, not a load
      additionalArguments: [`--plx-desktop-version=${app.getVersion()}`],
    },
  });
  win.once("ready-to-show", () => {
    if (startHidden && !shownOnce) hiddenSince = Date.now();
    else win.show();
  });
  // closing hides: the app stays loaded (and in the tray / dock) until Quit
  win.on("close", (e) => {
    if (quitting) return;
    e.preventDefault();
    win.hide();
  });
  win.on("session-end", () => {
    quitting = true; // Windows is signing out or shutting down: a hidden window must not hold that up
  });
  win.on("hide", () => {
    hiddenSince = Date.now();
  });
  win.on("show", () => {
    shownOnce = true;
    hiddenSince = null;
  });
  // a minimized window is out of sight too: an update waits the same 10 minutes, then restarts
  win.on("minimize", () => {
    hiddenSince = Date.now();
  });
  win.on("restore", () => {
    hiddenSince = null;
  });
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
        { label: "Change server…", click: () => { serverChanged(null); openStart(); } },
        ...(app.isPackaged ? [{ label: "Open when I sign in to this computer", type: "checkbox", checked: loginItemOn(), click: (item) => setLoginItem(item.checked) }] : []),
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
let updateReady = false;
const UPDATE_HIDDEN_MS = 10 * 60 * 1000; // the window hidden this long: nobody is looking, so restart into the update
const UPDATE_CHECK_MS = 4 * 3600 * 1000; // the app no longer quits each night: look for an update every few hours

/** An app that never quits would never update: with an update downloaded, restart into it once the
 * window has been hidden a while (relaunching hidden, the store is on disk and survives). */
function installWhenIdle() {
  if (!updateReady || quitting || !updater) return;
  if (win && !win.isDestroyed() && win.isVisible()) return;
  if (hiddenSince !== null && Date.now() - hiddenSince < UPDATE_HIDDEN_MS) return;
  try {
    fs.writeFileSync(relaunchMarker(), "1", { mode: 0o600 });
    getStore().flush();
  } catch (e) {
    console.log(`update: relaunch marker not written (${e && e.code ? e.code : "error"})`);
  }
  quitting = true;
  updater.quitAndInstall(true, true);
}
function getUpdater() {
  if (updater) return updater;
  const { autoUpdater } = require("electron-updater");
  autoUpdater.autoDownload = true;
  autoUpdater.autoInstallOnAppQuit = true;
  autoUpdater.on("update-downloaded", () => {
    updateReady = true; // installed once the window has been hidden UPDATE_HIDDEN_MS (`installWhenIdle`), else when the app quits
    installWhenIdle();
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
    return tell(`Version ${next} is downloading; it installs by itself when the window has been closed for a few minutes, or when you quit.`);
  }
  return tell(`You have the latest version (${app.getVersion()}).`);
}

// ---- the page's two requests
let recent = [];
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
  serverChanged(origin);
  openStart();
  return { ok: true };
});

// ---- the page store's calls (the sender's origin is checked in each: see store-ipc.js)
const storeCalls = storeIpc.handlers({ store: { open: () => getStore().open(), get: (k) => getStore().get(k), put: (a) => getStore().put(a), drop: (k) => getStore().drop(k), wipe: (r) => getStore().wipe(r) }, getWin: () => win, getOrigin: () => serverOrigin });
for (const name of ["open", "get", "put", "drop", "wipe"]) ipcMain.handle(`desktop:store-${name}`, (e, arg) => storeCalls[name](e, arg));

// ---- open at login (offered once), and the tray: the app lives on with its window hidden
function loginItemOn() {
  try {
    return app.getLoginItemSettings().openAtLogin;
  } catch {
    return false;
  }
}
function setLoginItem(on) {
  try {
    app.setLoginItemSettings({ openAtLogin: !!on, args: ["--hidden"] });
  } catch (e) {
    console.log(`login item not set (${e && e.message ? String(e.message).slice(0, 60) : "error"})`);
  }
}
async function offerLoginItem() {
  if (!app.isPackaged || readConfig().loginAsked || !win) return;
  const { response } = await dialog.showMessageBox(win, {
    type: "question",
    buttons: ["Open when I sign in", "Not now"],
    defaultId: 0,
    cancelId: 1,
    title: "PLX Workbench",
    message: "Open the Workbench when you sign in to this computer?",
    detail: "It opens in the background, so the Workbench is ready the moment you click its icon. You can change this under File.",
  });
  writeConfig({ loginAsked: true });
  if (response === 0) setLoginItem(true);
}
let tray = null;
function makeTray() {
  if (process.platform === "darwin" || tray) return; // the Dock icon does this on a Mac
  try {
    const icon = nativeImage.createFromPath(path.join(__dirname, "..", "build", "icon.png")).resize({ width: 16, height: 16 });
    tray = new Tray(icon);
    tray.setToolTip("PLX Workbench");
    tray.setContextMenu(Menu.buildFromTemplate([{ label: "Open the Workbench", click: show }, { label: "Quit", click: () => app.quit() }]));
    tray.on("click", show);
  } catch (e) {
    console.log(`tray not made (${e && e.message ? String(e.message).slice(0, 60) : "error"})`);
  }
}

if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on("second-instance", show);
  app.whenReady().then(() => {
    // no permission is granted to a page; notifications go through the one IPC door above
    session.defaultSession.setPermissionRequestHandler((_wc, _perm, cb) => cb(false));
    session.defaultSession.setPermissionCheckHandler(() => false);
    startHidden = process.argv.includes("--hidden") || (process.platform === "darwin" && app.getLoginItemSettings().wasOpenedAtLogin) || consumeRelaunchMarker();
    serverOrigin = readServer();
    watchAnswers();
    buildMenu();
    createWindow();
    makeTray();
    void checkForUpdates(false);
    setInterval(() => void checkForUpdates(false), UPDATE_CHECK_MS).unref();
    setInterval(installWhenIdle, 60_000).unref();
    if (!startHidden) win.once("show", () => setTimeout(() => void offerLoginItem(), 5000));
    app.on("activate", () => {
      if (!win) createWindow();
      else show();
    });
  });
  // the window hides when closed; only Quit (menu, tray, the OS) ends the app, and then the index is written
  app.on("before-quit", () => {
    quitting = true;
    if (store) store.flush();
  });
}
