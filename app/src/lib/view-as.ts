// The owner's "view as <person>" (operator_api/view_as.py): while it is on, every read the
// app makes carries X-PLX-View-As and the server answers as that person; every write is
// refused here before it leaves the browser, and again by the server. Kept for this tab
// only (sessionStorage), so a new tab or a reload in another tab is the owner's own view.

export const VIEW_AS_HEADER = "X-PLX-View-As";
const KEY = "plx-view-as";

export type ViewingAs = { key: string; name: string };

let current: ViewingAs | null | undefined;
const listeners = new Set<() => void>();

function load(): ViewingAs | null {
  if (current !== undefined) return current;
  current = null;
  try {
    const raw = typeof sessionStorage === "undefined" ? null : sessionStorage.getItem(KEY);
    const v = raw ? (JSON.parse(raw) as ViewingAs) : null;
    if (v && typeof v.key === "string" && /^[0-9a-f]{20}$/.test(v.key) && typeof v.name === "string") current = v;
  } catch {
    current = null;
  }
  return current;
}

export function viewingAs(): ViewingAs | null {
  return load();
}

export function setViewingAs(v: ViewingAs | null) {
  current = v;
  try {
    if (v) sessionStorage.setItem(KEY, JSON.stringify(v));
    else sessionStorage.removeItem(KEY);
  } catch {
    /* kept in memory only */
  }
  for (const f of listeners) f();
}

export function onViewingAs(f: () => void): () => void {
  listeners.add(f);
  return () => listeners.delete(f);
}

/** Paths that stay the owner's own while viewing: who is signed in, sign-in, the people list, health and ops. */
export function ownPath(path: string): boolean {
  // "me" and "health" end there: /api/meetings is a read of the viewed person's (which the
  // server refuses while viewing), never the owner's own shown under their name
  return /^\/api\/(?:(?:me|health)(?:[/?#]|$)|auth\/|view-as\/|ops\/)/.test(path);
}
