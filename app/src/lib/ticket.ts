"use client";

// A demo access link carries its ticket in the URL fragment (#ticket=…), which
// browsers never send to a server. It is read once, on whatever page the link
// opened, and the fragment is removed from the address bar and history at once,
// so it cannot linger in a bookmark, a screenshot or a shared link.

let captured: string | null = null;


/** Strip any #ticket= or ?ticket= from the current URL; remember its value for sign-in. */
export function captureTicket(): void {
  if (typeof window === "undefined") return;
  const w = window as unknown as { __plxTicket?: string };
  if (typeof w.__plxTicket === "string") {
    captured = w.__plxTicket || null;
    delete w.__plxTicket;
  }
  const hash = window.location.hash;
  if (/(?:^#|&)ticket=/.test(hash)) {
    const m = hash.match(/(?:^#|&)ticket=([^&]*)/);
    try {
      captured = m && m[1] ? decodeURIComponent(m[1]) : null;
    } catch {
      captured = null;
    }
    window.history.replaceState(window.history.state, "", window.location.pathname + window.location.search);
    return;
  }
  const search = window.location.search;
  if (/(?:^\?|&)ticket=/.test(search)) {
    const params = new URLSearchParams(search);
    const val = params.get("ticket");
    captured = val || null;
    params.delete("ticket");
    const nextSearch = params.toString();
    window.history.replaceState(
      window.history.state,
      "",
      window.location.pathname + (nextSearch ? `?${nextSearch}` : "") + window.location.hash,
    );
  }
}

/** The captured ticket, once; later calls return null. */
export function takeTicket(): string | null {
  const t = captured;
  captured = null;
  return t;
}

/** True while an access link's ticket is on this page and not yet used: who reads is not yet
 * settled, so the page's reads wait for the sign-in check as before. */
export function ticketComing(): boolean {
  if (captured) return true;
  if (typeof window === "undefined") return false;
  return (
    typeof (window as unknown as { __plxTicket?: string }).__plxTicket === "string" ||
    /(?:^#|&)ticket=/.test(window.location.hash) ||
    /(?:^\?|&)ticket=/.test(window.location.search)
  );
}
