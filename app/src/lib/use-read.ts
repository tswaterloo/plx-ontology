"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { onSessionReset } from "./session-cache";
import { viewingAs } from "./view-as";

// Stale-while-revalidate for a page's main read: a page read before in this tab paints at once
// from what it showed last time, and is read again behind it. Kept for the page's life only,
// per person (dropped on sign-out or another sign-in) and per view-as.

// `shown` is false for a value read ahead (a link hovered) that no page has shown yet: the
// page then shows it without reading it again, since it was read a moment ago.
const kept = new Map<string, { value: unknown; at: number; shown: boolean }>();
const ahead = new Map<string, Promise<unknown>>();
const MOST_KEPT = 50;
/** How long a value read ahead counts as current for the page it was read for. */
export const AHEAD_FRESH_MS = 15_000;
onSessionReset(() => {
  kept.clear();
  ahead.clear();
});

/** Forget every kept read (tests start each case from nothing). */
export function forgetKeptReads(): void {
  kept.clear();
  ahead.clear();
}

function keyOf(key: string): string {
  return `${viewingAs()?.key ?? ""} ${key}`;
}

/** What `key` showed last time in this tab, if anything. */
export function keptRead<T>(key: string): T | null {
  return (kept.get(keyOf(key))?.value as T | undefined) ?? null;
}

/** Keep what `key` read, the newest last (the oldest goes past MOST_KEPT). */
export function keepRead(key: string, value: unknown, shown = true): void {
  const k = keyOf(key);
  kept.delete(k);
  kept.set(k, { value, at: Date.now(), shown });
  if (kept.size > MOST_KEPT) kept.delete(kept.keys().next().value as string);
}

/** Read `key` ahead (a link to its page is hovered or focused), once: nothing when it was
 * read a moment ago or is being read. A failure is dropped; the page reads again. */
export function readAhead(key: string, load: (signal: AbortSignal) => Promise<unknown>): void {
  const k = keyOf(key);
  const have = kept.get(k);
  if (ahead.has(k) || (have && Date.now() - have.at < AHEAD_FRESH_MS)) return;
  let read: Promise<unknown>;
  try {
    read = load(new AbortController().signal);
  } catch {
    return;
  }
  const p = read.then((v) => {
    if (ahead.get(k) === p) keepRead(key, v, false);
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
export function useRead<T>(key: string, load: (signal: AbortSignal) => Promise<T>) {
  const [state, setState] = useState<{ key: string; data: T | null }>(() => ({ key, data: keptRead<T>(key) }));
  const [error, setError] = useState<unknown>(null);
  const [round, setRound] = useState(0);
  const loadRef = useRef(load);
  loadRef.current = load;
  // a new key paints its own kept value (or nothing), never the last key's
  let data = state.data;
  if (state.key !== key) {
    data = keptRead<T>(key);
    setState({ key, data });
  }
  useEffect(() => {
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
    const pending = round === 0 ? ahead.get(k) : undefined;
    ((pending as Promise<T> | undefined) ?? loadRef.current(ctl.signal)).then(
      (v) => {
        if (ctl.signal.aborted) return;
        keepRead(key, v);
        setState({ key, data: v });
      },
      (e) => {
        if (!ctl.signal.aborted) setError(e);
      },
    );
    return () => ctl.abort();
  }, [key, round]);
  const retry = useCallback(() => setRound((n) => n + 1), []);
  const set = useCallback((v: T) => {
    keepRead(key, v);
    setState({ key, data: v });
  }, [key]);
  return { data, error, retry, set };
}
