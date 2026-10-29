"use client";

// A demo access link carries its ticket in the URL fragment (#ticket=…), which
// browsers never send to a server. It is read once, on whatever page the link
// opened, and the fragment is removed from the address bar and history at once,
// so it cannot linger in a bookmark, a screenshot or a shared link.

let captured: string | null = null;


/** Strip any #ticket= from the current URL; remember its value for sign-in. */
export function captureTicket(): void {
  if (typeof window === "undefined") return;
  const w = window as unknown as { __plxTicket?: string };
  if (typeof w.__plxTicket === "string") {
    captured = w.__plxTicket || null;
    delete w.__plxTicket;
  }
  const hash = window.location.hash;
  if (!/(?:^#|&)ticket=/.test(hash)) return;
  const m = hash.match(/(?:^#|&)ticket=([^&]*)/);
  try {
    captured = m && m[1] ? decodeURIComponent(m[1]) : null;
  } catch {
    captured = null;
  }
  window.history.replaceState(window.history.state, "", window.location.pathname + window.location.search);
}

/** The captured ticket, once; later calls return null. */
export function takeTicket(): string | null {
  const t = captured;
  captured = null;
  return t;
}
