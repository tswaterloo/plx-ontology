"use strict";
// The only door between the Workbench page and this machine. The page can ask for a
// notification and hear a menu choice; it gets no file, process or network access from here.
const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("plxDesktop", {
  /** True in this app, absent in a browser. */
  app: true,
  version: ipcRenderer.sendSync("desktop:version"),
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
});
