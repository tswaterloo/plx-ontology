// Shared by the server layout (which inlines it) and tests; not a client module.

/**
 * Runs inline in <head> (nonce-stamped, before Next.js hydrates and records the
 * URL): moves a #ticket= value into memory and removes the fragment. Kept
 * self-contained because it is serialised into the page.
 */
export const TICKET_BOOT_SCRIPT = `(function(){try{var h=location.hash,m=h.match(/(?:^#|&)ticket=([^&]*)/);if(!m)return;window.__plxTicket=decodeURIComponent(m[1]||"");history.replaceState(history.state,"",location.pathname+location.search)}catch(e){}})();`;
