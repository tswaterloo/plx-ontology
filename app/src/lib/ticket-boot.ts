import { DOOR_KINDS } from "./format";

// Shared by the server layout (which inlines it) and tests; not a client module.

/**
 * Runs inline in <head> (nonce-stamped, before Next.js hydrates and records the
 * URL): moves a #ticket= value into memory and removes the fragment. Kept
 * self-contained because it is serialised into the page.
 */
export const TICKET_BOOT_SCRIPT = `(function(){try{var h=location.hash,m=h.match(/(?:^#|&)ticket=([^&]*)/);if(!m)return;window.__plxTicket=decodeURIComponent(m[1]||"");history.replaceState(history.state,"",location.pathname+location.search)}catch(e){}})();`;

/**
 * Marks the page with the reader's system before it paints (<html data-os="win|mac|other">):
 * Windows draws text with ClearType, so its body text is set a touch heavier and its greys a
 * touch darker (globals.css).
 */
export const OS_BOOT_SCRIPT = `(function(){try{var u=navigator.userAgent;document.documentElement.setAttribute("data-os",/Windows/.test(u)?"win":/Macintosh|Mac OS X|iPhone|iPad/.test(u)?"mac":"other")}catch(e){}})();`;

/**
 * Starts the sign-in check's two reads (/api/auth/config, /api/me) while the page's scripts
 * are still loading, instead of one after the other once React has started (measured 11-22:
 * they began 170-270 ms after the page's first byte and every page read waited on them).
 * Both are the reader's own (no view-as header applies to either). Not when an access link's
 * ticket is being handed over: that sign-in may change who the session is, so its check is
 * read after it, as before. `lib/api.ts` (`takeEarly`) uses each answer once, only while young.
 * It also starts the sidebar read and, for the pages listed in its map, the page's main read
 * (W1; each URL must equal the fetch the page makes, `getJson` takes it once as `r:<path>`).
 * Runs after TICKET_BOOT_SCRIPT, nonce-stamped; self-contained because it is serialised.
 */
/** Each page's main read, started with the HTML: the route's path to the read's URL, which must equal the
 * fetch the page makes (lib/api.ts). One list, here, for the script and the tests. */
export const EARLY_ROUTE_READS: Readonly<Record<string, string>> = {
  "/assist": "/api/autopilot",
  "/ops": "/api/ops/status",
  "/trust": "/api/trust",
  "/templates": "/api/templates",
  "/data-health": "/api/data-health",
  "/memory": "/api/memory",
  "/meetings": "/api/brief/meetings",
  "/records": "/api/records/views",
  "/more": "/api/records/views",
  "/activity": "/api/actions/log",
  "/team": "/api/team",
};
export const EARLY_READS_SCRIPT = `(function(){try{if(typeof window.__plxTicket==="string"||/(?:^#|&)ticket=/.test(location.hash)||typeof fetch!=="function")return;var f=function(p){return fetch(p,{credentials:"same-origin",headers:{Accept:"application/json"}}).then(function(r){if(!r.ok)return{ok:false,status:r.status};return r.json().then(function(b){return{ok:true,status:r.status,body:b}})}).catch(function(){return{ok:false,status:0}})};var E={at:Date.now(),config:f("/api/auth/config"),me:f("/api/me")};E["r:/api/sidebar"]=f("/api/sidebar");var M=${JSON.stringify(EARLY_ROUTE_READS)}[location.pathname];if(M)E["r:"+M]=f(M);var O=/^\\/object\\/([A-Za-z][A-Za-z0-9_]{0,59})\\/([^\\/]+)\\/?$/.exec(location.pathname);if(O){var id;try{id=decodeURIComponent(O[2])}catch(e){id=O[2]}if(id&&id.length<=700){var b="/api/object/"+O[1]+"/"+encodeURIComponent(id);E["r:"+b+"?quick=1&core=1"]=f(b+"?quick=1&core=1");E["r:"+b+"?quick=1"]=f(b+"?quick=1");var D=${JSON.stringify(DOOR_KINDS)}[O[1]];if(D&&id.indexOf(O[1]+":")===0){var bz="/api/biz/page?"+new URLSearchParams({kind:D,record:id}).toString();E["r:"+bz]=f(bz)}}}window.__plxEarly=E}catch(e){}})();`;
