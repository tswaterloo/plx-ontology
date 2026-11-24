"use client";

// The page's side of the desktop page store (SPEED-SPECS 10.9). Two tiers, one read path:
//  - hot: the `kept` map of use-read.ts, by API path, read synchronously so a page draws a
//    stored answer in the click's own render;
//  - durable: the desktop shell's main process (`desktop/src/store.js`), written off the click
//    path with the server's body text and `X-PLX-Door`, read back at launch.
// The page stays the only thing that asks the server (its cookie, its view-as header, its 401
// handling and its abort are unchanged). Nothing here decides what a person may see: it only
// replays what the server already sent this person on this machine, and forgets it all when
// the door value moves, the session is refused, the person signs out, or the shell says so.
//
// A browser (no desktop bridge) keeps the hot tier only.

import { desktopStore, type StoredAnswer } from "./desktop";
import { clearKeptReads, forgetKeptReads, hydrateRead, keptDoor, noteBytes, setKeptDoor } from "./use-read";
import { onSessionReset, resetSessionCaches } from "./session-cache";
import { viewingAs } from "./view-as";

/** This build's identity: an answer kept by another build is read again, never drawn. */
export const RELEASE = process.env.NEXT_PUBLIC_PLX_RELEASE || "dev";
export const DOOR_HEADER = "X-PLX-Door";
export const SESSION_HEADER = "X-PLX-Session";

/** Reads whose answers are not kept (identity and sign-in stay live; streams are never GETs here). */
const NEVER_KEPT = ["/api/auth/", "/api/me", "/api/health", "/api/ask", "/api/events", "/api/view-as/"];
export function keptPath(path: string): boolean {
  const p = path.split("?")[0];
  return p.startsWith("/api/") && !NEVER_KEPT.some((x) => (x.endsWith("/") ? p.startsWith(x) : p === x || p.startsWith(`${x}/`)));
}

// the durable tier is best effort: a failed write or read leaves the page on its memory copy and
// the server's answer, and is said in the console (once per kind), never swallowed
const said = new Set<string>();
function note(what: string, e: unknown): void {
  if (said.has(what)) return;
  said.add(what);
  console.warn(`page store: ${what}: ${e instanceof Error ? e.message : String(e)}`);
}

/** An answer that is still being read or is known out of date is shown, never kept. ONE definition
 * (the record pane's reuse in api.ts asks it too). */
export function isSettledAnswer(body: unknown): boolean {
  const flags = body as { stale_since?: string | null; reading?: boolean | null } | null;
  return !(flags?.stale_since || flags?.reading);
}

/** The page's own copy is dropped, and the shell's too: a refused session or a moved door. */
let shellWiped = false; // the shell emptied its store itself: the reset it caused must not wipe again
function forgetEverything(fromShell: boolean) {
  shellWiped = fromShell;
  try {
    resetSessionCaches(); // the hot tier, and the shell's store (below), with every other kept read
  } finally {
    shellWiped = false;
  }
}
// sign-out, a different person, a refused session and a moved door all end in resetSessionCaches:
// the shell's store goes with the page's memory
onSessionReset(() => {
  if (!shellWiped) desktopStore()?.wipe().catch((e) => note("wipe", e));
});

/** Writes that change nothing a kept read shows (a question asked, a sign-in, a preview, a note
 * of feedback, a read mark): they leave the hot tier alone. Every other write empties it. */
const WRITES_THAT_KEEP = ["/api/ask", "/api/auth/", "/api/feedback", "/api/actions/preview", "/api/briefing/seen", "/api/doc-fill", "/api/charts/", "/api/view-as/", "/api/watch/rules/compile", "/api/templates/propose", "/api/statement/read", "/api/instruct/", "/api/insight-news/"];
export function writeKeepsReads(path: string): boolean {
  const p = path.split("?")[0];
  return WRITES_THAT_KEEP.some((x) => p === x || p.startsWith(x.endsWith("/") ? x : `${x}/`));
}

/** Your own change was sent (api.ts `call`, before it goes and again when it answers): the hot tier
 * forgets every kept answer, so none older than the change is drawn. Unknown writes clear (fail closed). */
export function noteOwnWrite(path: string): void {
  if (!writeKeepsReads(path)) clearKeptReads();
}

/** Every API answer's headers (api.ts `call`), before its body is used. */
export function noteAnswerHeaders(headers: Headers): void {
  if (headers.get(SESSION_HEADER) === "refused") {
    forgetEverything(false);
    return;
  }
  const door = headers.get(DOOR_HEADER);
  if (!door) return; // the server said nothing about the door: nothing is kept from this answer
  const held = keptDoor();
  if (held !== null && held !== door) forgetEverything(false);
  setKeptDoor(door);
}

let started = false;
let opened: Promise<{ durable: boolean; door: string | null; keys: string[]; entries: StoredAnswer[] } | null> | null = null;
let signedIn = false;

/** After a GET answered 200: keep it (durable tier), off the click path. Never a view-as answer. */
export function rememberAnswer(path: string, text: string, door: string | null, body: unknown): void {
  noteBytes(path, text.length);
  const st = desktopStore();
  if (!st || !door || viewingAs() || !keptPath(path) || !isSettledAnswer(body)) return;
  const entry = { key: path, body: text, door, release: RELEASE, fetchedAt: Date.now() };
  const run = () => void st.put(entry).catch((e) => note("put", e));
  if (typeof requestIdleCallback === "function") requestIdleCallback(run, { timeout: 2000 });
  else setTimeout(run, 0);
}

/** A read answered 404: that stored entry goes. (The shell drops it too; this covers the hot tier.) */
export function forgetAnswer(path: string): void {
  void desktopStore()?.drop(path).catch((e) => note("drop", e));
}

function take(a: { key: string; body: string; fetchedAt: number; release: string; door: string }): void {
  if (a.release !== RELEASE) return; // another build's answer: read again, never drawn
  try {
    hydrateRead(a.key, JSON.parse(a.body), a.fetchedAt, a.body.length);
  } catch {
    void desktopStore()?.drop(a.key).catch((e) => note("drop", e));
  }
}

/** Once per page load, in the desktop app: draw-ready answers from the store for the first
 * screen, then the rest by recent use in idle time. Nothing is awaited by a page. */
export function startReadStore(): void {
  if (started) return;
  const st = desktopStore();
  if (!st) return;
  started = true;
  st.onWiped(() => forgetEverything(true));
  opened = st.open().catch((e) => (note("open", e), null));
  void drawStored();
}

/** The person's own session answered (/api/me): only now may stored answers be drawn, and only
 * when the server's door value is the one they were kept under. Before it, nothing is drawn. */
export function storeMayDraw(): void {
  signedIn = true;
  void drawStored();
}

/** The person leaves (or tries to): the shell's store goes first, whether or not the server answers. */
export function wipeStoreNow(): void {
  void desktopStore()?.wipe().catch((e) => note("wipe", e));
}

async function drawStored(): Promise<void> {
  const st = desktopStore();
  if (!st || !opened || !signedIn) return;
  const pending = opened;
  opened = null; // once: a second caller finds nothing to draw
  const o = await pending;
  if (!o) return;
  void Promise.resolve(o)
    .then(async (o) => {
      if (!o.durable || !o.door) return;
      const seen = keptDoor();
      // drawn only when the server's own answer named the very door the entries were kept under; an
      // answer with no door (unknown), or another door, draws nothing (fails closed)
      if (seen === null || seen !== o.door) return;
      setKeptDoor(o.door);
      for (const e of o.entries) take(e);
      const have = new Set(o.entries.map((e) => e.key));
      const rest = o.keys.filter((k) => !have.has(k)).slice(0, 60);  // guard-ok: a warm-up bound on kept entries drawn at start; any other key is still read from the store when its page asks
      for (let i = 0; i < rest.length; i += 4) {
        await new Promise<void>((r) => (typeof requestIdleCallback === "function" ? requestIdleCallback(() => r(), { timeout: 1500 }) : setTimeout(r, 50)));
        const got = await Promise.all(rest.slice(i, i + 4).map((k) => st.get(k).catch((e) => (note("get", e), null))));
        for (const g of got) if (g) take(g);
      }
    })
    .catch((e) => note("open", e));
}

/** Tests start each case from nothing. */
export function resetReadStoreForTests(): void {
  started = false;
  opened = null;
  signedIn = false;
  forgetKeptReads();
}
