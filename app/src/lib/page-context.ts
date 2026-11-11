// The page the Ask box is opened on, carried to /ask so a line typed there ("reply to
// this", "chase this supplier") works on what the person has open: a record page gives its
// record, an opened source its citation, an autopilot item its id, a meeting its id, and any
// other page its path.

/** The query string (from "&") naming the page at `pathname` + `search` for /ask. */
export function pageContextQuery(pathname: string, search: string): string {
  const params = new URLSearchParams(search);
  const object = /^\/object\/[^/]+\/(.+)$/.exec(pathname);
  if (object) return `&object=${encodeURIComponent(decodeURIComponent(object[1]))}`;
  if (pathname === "/source" && params.get("handle")) return `&source=${encodeURIComponent(params.get("handle") as string)}`;
  if (params.get("item") && /^\/(autopilot|my-work)\b/.test(pathname)) return `&item=${encodeURIComponent(params.get("item") as string)}`;
  if (params.get("meeting") && pathname.startsWith("/meetings")) return `&meeting=${encodeURIComponent(params.get("meeting") as string)}`;
  if (pathname === "/ask" || pathname === "/") return "";
  return `&page=${encodeURIComponent(pathname)}`;
}

/** The current window's page, for a click handler. */
export function currentPageQuery(): string {
  return typeof window === "undefined" ? "" : pageContextQuery(window.location.pathname, window.location.search);
}

// A line about what is open ("this", "here", "it") or an imperative to do something
// ("chase the supplier") takes the page along; a plain question ("what ships this week",
// "can you tell me when PO 1234 ships?") is asked of everything the person may read.
const POINTS = /\b(this|that|these|those|here|it)\b/i;
const DOES =
  /^(?:(?:please|pls|ok|okay)[,\s]+)*(?:(?:can|could|would|will) you\s+(?:please\s+)?)?(?:reply|respond|draft|write|send|email|chase|nudge|tell|ping|message|notify|remind|set|update|change|move|add|create|make|prepare|file|upload|schedule|book|forward|assign|cancel|log|note|approve|decline|share|follow up)\b(?!\s+me\b)/i;

/** Whether the typed line is scoped to the page it was typed on. */
export function scopedToPage(line: string): boolean {
  const text = line.trim();
  return POINTS.test(text) || DOES.test(text);
}
