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
