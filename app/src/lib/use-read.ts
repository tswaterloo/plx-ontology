"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { quietly } from "./quiet";
import { onSessionReset } from "./session-cache";
import { viewingAs } from "./view-as";

// Stale-while-revalidate for a page's main read: a page read before in this tab paints at once
// from what it showed last time, and is read again behind it. Per person (dropped on sign-out
// or another sign-in) and per view-as. In the desktop app the map is also filled at launch from
// the page store (`read-store.ts`), so the first visit of a session can paint from disk; the
// answer is then drawn with its age (`at`) until the read behind it returns.
//
// The key is the read's API path (`/api/trust`), the same name the page store and the
// read-ahead route table use.

// `shown` is false for a value read ahead (a link hovered) that no page has shown yet: the
// page then shows it without reading it again, since it was read a moment ago.
const kept = new Map<string, { value: unknown; at: number; shown: boolean; bytes: number }>();
const ahead = new Map<string, Promise<unknown>>();
// Reads a mounted page can revalidate when the server's event stream says data moved.
// Keys are the page's own API paths (not the view-as-prefixed hot-tier keys).
const activeRetries = new Map<string, Set<(server?: boolean) => void>>();
/** The hot tier is bounded by bytes (an answer's length when known), oldest first out. */
export const MOST_KEPT_BYTES = 48 * 1024 * 1024;
const DEFAULT_BYTES = 100_000;
let keptBytes = 0;
// the length of each path's last answer (api.ts `getJson` says it), for the byte bound
const sizes = new Map<string, number>();
export function noteBytes(key: string, n: number): void {
  sizes.set(key, n);
}
let door: string | null = null; // the server's door value the kept reads were read under
/** How long a value read ahead counts as current for the page it was read for. */
export const AHEAD_FRESH_MS = 15_000;
onSessionReset(() => {
  kept.clear();
  ahead.clear();
  keptBytes = 0;
  door = null;
  sizes.clear();
});

/** The door value the kept reads belong to (null: none known). */
export const keptDoor = (): string | null => door;
export function setKeptDoor(d: string | null): void {
  door = d;
}

/** Your own change was sent: every kept answer may now be older than the change, so the hot
 * tier is emptied (the door stays: it is about who, not what). A page already drawn keeps what it
 * shows until its read behind returns, and says so (`at`); any page opened next reads fresh. */
let epoch = 0;
/** Counts own writes: a read that started before the count moved may carry an answer older than the change. */
export const writeEpoch = (): number => epoch;
export function clearKeptReads(): void {
  epoch += 1;
  kept.clear();
  ahead.clear();
  keptBytes = 0;
}

/** Forget every kept read (tests start each case from nothing). */
export function forgetKeptReads(): void {
  kept.clear();
  ahead.clear();
  keptBytes = 0;
  door = null;
}

function keyOf(key: string): string {
  return `${viewingAs()?.key ?? ""} ${key}`;
}


/** Register a mounted read's revalidation; returns the unregister. */
export function noteActiveRead(key: string, retry: (server?: boolean) => void): () => void {
  let set = activeRetries.get(key);
  if (!set) activeRetries.set(key, (set = new Set()));
  set.add(retry);
  return () => {
    set.delete(retry);
    if (!set.size) activeRetries.delete(key);
  };
}

/** API paths a mounted page can re-read now. */
export function activeReadKeys(): string[] {
  return [...activeRetries.keys()];
}

/** Ask every mounted read to revalidate (its shown answer stays age-labelled until then). */
export function rereadActive(): void {
  for (const retries of activeRetries.values()) for (const retry of retries) retry(true);
}

/** API paths with a hot-tier answer, owner and view-as copies de-duplicated. */
export function keptKeys(): string[] {
  return [...new Set([...kept.keys()].map((k) => {
    const at = k.indexOf(" /api/");
    if (at >= 0) return k.slice(at + 1);
    return k.startsWith(" ") ? k.slice(1) : k;
  }))];
}

/** What `key` showed last time in this tab (or the page store), if anything. */
export function keptRead<T>(key: string): T | null {
  return (kept.get(keyOf(key))?.value as T | undefined) ?? null;
}

/** When `key`'s kept answer was read (ms), or null. */
export function keptAt(key: string): number | null {
  return kept.get(keyOf(key))?.at ?? null;
}

/** Keep what `key` read, the newest last (the oldest go past MOST_KEPT_BYTES). `bytes`: its length when known. */
export function keepRead(key: string, value: unknown, shown = true, bytes?: number, at = Date.now()): void {
  bytes = bytes ?? sizes.get(key) ?? DEFAULT_BYTES;
  const k = keyOf(key);
  const had = kept.get(k);
  if (had) keptBytes -= had.bytes;
  kept.delete(k);
  kept.set(k, { value, at, shown, bytes });
  keptBytes += bytes;
  while (keptBytes > MOST_KEPT_BYTES && kept.size > 1) {
    const oldest = kept.keys().next().value as string;
    keptBytes -= kept.get(oldest)?.bytes ?? 0;
    kept.delete(oldest);
  }
}

/** An answer from the page store, for the owner's own view (never a view-as one): drawn at once,
 * read again by the page that shows it (it is older than AHEAD_FRESH_MS). Not over a newer one. */
export function hydrateRead(key: string, value: unknown, at: number, bytes: number): void {
  if (viewingAs()) return;
  const have = kept.get(keyOf(key));
  if (have && have.at >= at) return;
  keepRead(key, value, true, bytes, at);
}

/** Read `key` ahead (a link to its page is hovered or focused), once: nothing when it was
 * read a moment ago or is being read. A failure is dropped; the page reads again. */
export function readAhead(key: string, load: (signal: AbortSignal) => Promise<unknown>, force = false): void {
  const k = keyOf(key);
  const have = kept.get(k);
  if (ahead.has(k) || (!force && have && Date.now() - have.at < AHEAD_FRESH_MS)) return;
  const began = epoch;
  let read: Promise<unknown>;
  try {
    read = load(new AbortController().signal);
  } catch {
    return;
  }
  const p = read.then((v) => {
    if (ahead.get(k) === p && began === epoch) keepRead(key, v, false); // not an answer read before a change of yours
    return v;
  });
  ahead.set(k, p);
  const gone = () => {
    if (ahead.get(k) === p) ahead.delete(k);
  };
  p.then(gone, gone);
}

/**
 * A page's main read: the kept value shows at once and `load` reads it again; a new value
 * replaces it, an error shows as before (data read earlier is not passed off as current).
 * `retry` reads again. `load` may change between renders; `key` names the read.
 */
export function useRead<T>(key: string, load: (signal: AbortSignal) => Promise<T>, on = true) {
  // `at`: when the shown answer was read, until the read behind it returns (then null)
  const [state, setState] = useState<{ key: string; data: T | null; at: number | null }>(() => ({ key, data: keptRead<T>(key), at: keptRead<T>(key) === null ? null : keptAt(key) }));
  const [error, setError] = useState<unknown>(null);
  const [round, setRound] = useState(0);
  const loadRef = useRef(load);
  const running = useRef(false);   // a read is under way: a retry never aborts it or overlaps it
  const queued = useRef(false);    // a server event/manual retry asked while it ran: read again after it
  const abortRetries = useRef(0);
  loadRef.current = load;
  // a new key paints its own kept value (or nothing), never the last key's
  let data = state.data;
  if (state.key !== key) {
    data = keptRead<T>(key);
    abortRetries.current = 0;
    setState({ key, data, at: data === null ? null : keptAt(key) });
  }
  useEffect(() => {
    if (!on) return;
    const ctl = new AbortController();
    setError(null);
    const k = keyOf(key);
    const have = kept.get(k);
    if (round === 0 && have && !have.shown && Date.now() - have.at < AHEAD_FRESH_MS) {
      // read ahead a moment ago and not shown yet: it is current, no second request
      have.shown = true;
      return;
    }
    if (have) have.shown = true;
    const began = epoch;
    const pending = round === 0 ? ahead.get(k) : undefined;
    running.current = true;
    ((pending as Promise<T> | undefined) ?? (have ? quietly(() => loadRef.current(ctl.signal)) : loadRef.current(ctl.signal))).then(
      (v) => {
        running.current = false;
        if (ctl.signal.aborted) return;
        if (queued.current) {
          queued.current = false;
          setRound((n) => n + 1);   // that answer may predate the server's change; never show it as current
          return;
        }
        if (began !== epoch) {
          // a change of yours was sent after this read started: its answer may be the older one; read again
          setRound((n) => n + 1);
          return;
        }
        keepRead(key, v);
        abortRetries.current = 0;
        shownAt.current = Date.now();
        setState({ key, data: v, at: null });
      },
      (e) => {
        running.current = false;
        if (ctl.signal.aborted) return;
        if (queued.current) {
          queued.current = false;
          setRound((n) => n + 1);
          return;
        }
        // an internal abort (e.g. a sibling route resetting shared caches) is not a server error: retry cleanly once
        if (e instanceof DOMException && e.name === "AbortError" && abortRetries.current < 2) {
          abortRetries.current += 1;
          setRound((n) => n + 1);
          return;
        }
        setError(e);
      },
    );
    return () => {
      running.current = false;
      ctl.abort();
    };
  }, [key, round, on]);
  // a read again (Retry, or after a change of yours): what is shown is an earlier answer until the new one lands
  const shownAt = useRef<number | null>(null);
  const retry = useCallback((server = false) => {
    if (running.current) {
      if (server) queued.current = true;   // never overlap it; when it lands, read again
      return;
    }
    setState((s) => (s.data === null || s.at !== null ? s : { ...s, at: shownAt.current ?? Date.now() }));
    setRound((n) => n + 1);
  }, []);
  useEffect(() => noteActiveRead(key, retry), [key, retry]);
  const set = useCallback((v: T) => {
    keepRead(key, v);
    setState({ key, data: v, at: null });
  }, [key]);
  // `at`: the shown answer is an earlier one (the page store's, or this tab's), being read again; null once it is current
  return { data, error, retry, set, at: state.key === key ? state.at : null };
}
