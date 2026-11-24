"use strict";
// The only door between the Workbench page and this machine. The page can ask for a
// notification and hear a menu choice; it gets no file, process or network access from here.
const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("plxDesktop", {
  /** True in this app, absent in a browser. */
  app: true,
  // from the window's own start-up arguments: no synchronous call to the main process on each page load
  version: ((process.argv.find((a) => a.startsWith("--plx-desktop-version=")) || "").split("=")[1]) || "unknown",
  /** Show a system notification: { title, body, href } (href is a path inside the Workbench). */
  notify: (n) => ipcRenderer.send("desktop:notify", n),
  /** Hear a menu choice or a clicked notification: ({ action } | { href }). Returns the unsubscribe. */
  onMenu: (cb) => {
    if (typeof cb !== "function") return () => undefined;
    const handler = (_e, msg) => cb(msg);
    ipcRenderer.on("desktop:menu", handler);
    return () => ipcRenderer.removeListener("desktop:menu", handler);
  },
  /** First-run page only: remember the Workbench address. Resolves { ok } or { error }. */
  saveServer: (url) => ipcRenderer.invoke("desktop:save-server", url),
  /** The durable page store (main process, refuses any page but the Workbench's own). Every
   * answer is an opaque string the server already sent this person; the page decides what to put. */
  store: {
    open: () => ipcRenderer.invoke("desktop:store-open"),
    get: (key) => ipcRenderer.invoke("desktop:store-get", key),
    put: (entry) => ipcRenderer.invoke("desktop:store-put", entry),
    drop: (key) => ipcRenderer.invoke("desktop:store-drop", key),
    wipe: () => ipcRenderer.invoke("desktop:store-wipe"),
    /** The shell emptied the store (sign-out, refused session, another door): the page forgets its own copy too. */
    onWiped: (cb) => {
      if (typeof cb !== "function") return () => undefined;
      const handler = () => cb();
      ipcRenderer.on("desktop:wiped", handler);
      return () => ipcRenderer.removeListener("desktop:wiped", handler);
    },
  },
});
