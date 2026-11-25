"use client";

// The page's push stream (SPEED-SPECS 10.9 #7). One fetch stream per signed-in page,
// parsed by the same SSE parser as Ask. Events carry marks only: the open page re-reads
// at once, older hot-tier reads catch up spread over a minute, a moved door wipes what was
// kept, and a different release reloads the page. There is no timed polling here; the only
// timers are reconnect backoff and the catch-up spread.

import { ownerStreamHeaders, readPath } from "./api";
import { desktopStore } from "./desktop";
import { noteAnswerHeaders } from "./read-store";
import { onSessionReset } from "./session-cache";
import { SseParser } from "./sse";
import { activeReadKeys, keptKeys, readAhead, rereadActive } from "./use-read";
import { viewingAs } from "./view-as";

const FIRST_BACKOFF_MS = 1000;
const MOST_BACKOFF_MS = 30_000;
const CATCHUP_SPREAD_MS = 60_000;
const STREAM_STALL_MS = 45_000;   // heartbeats arrive every 20 s; silence beyond this is a dead connection
const MAX_CONCURRENT_CATCHUP = 4;
const IDLE_RELOAD_MS = 2000;

let running = false;
let stopped = true;
let current: AbortController | null = null;
let missed = false;
let releaseSeen: string | null = null;
let cachedUsedKeys: string[] | null = null;
let reloadPending = false;
const timers = new Set<ReturnType<typeof setTimeout>>();

const wait = (ms: number) => new Promise<void>((done) => setTimeout(done, ms));

function later(ms: number, run: () => void): void {
  const t = setTimeout(() => {
    timers.delete(t);
    run();
  }, ms);
  timers.add(t);
}

function isTyping(): boolean {
  if (typeof document === "undefined") return false;
  const el = document.activeElement;
  if (!el) return false;
  const tag = el.tagName;
  return tag === "INPUT" || tag === "TEXTAREA" || (el as HTMLElement).isContentEditable === true;
}

function scheduleReload(): void {
  if (reloadPending || typeof window === "undefined") return;
  reloadPending = true;
  if (typeof document !== "undefined" && document.hidden) {
    window.location.reload();
    return;
  }
  const onVis = () => {
    if (document.hidden) {
      document.removeEventListener("visibilitychange", onVis);
      window.location.reload();
    }
  };
  document.addEventListener("visibilitychange", onVis);
  const tryIdle = () => {
    if (stopped) {
      document.removeEventListener("visibilitychange", onVis);
      return;
    }
    if (document.hidden || !isTyping()) {
      document.removeEventListener("visibilitychange", onVis);
      window.location.reload();
      return;
    }
    later(IDLE_RELOAD_MS, tryIdle);
  };
  later(IDLE_RELOAD_MS, tryIdle);
}

/** Re-read what is on screen now, then older hot-tier reads a few at a time (most-recent-first,
 * bounded to MAX_CONCURRENT_CATCHUP in flight). `spread` is used after a reconnect, when events
 * may have been missed and every client must not return to the server in the same second. */
function scheduleKeys(keys: string[], spread: boolean, skip: Set<string>): void {
  const todo: string[] = [];
  for (const key of keys) {
    if (skip.has(key) || !key.startsWith("/api/")) continue;
    skip.add(key);
    todo.push(key);
  }
  if (todo.length === 0) return;

  let active = 0;
  let idx = 0;
  const pump = () => {
    while (active < MAX_CONCURRENT_CATCHUP && idx < todo.length) {
      const key = todo[idx++];
      active++;
      const jitter = spread ? Math.floor(Math.random() * CATCHUP_SPREAD_MS) : 0;
      later(jitter + (idx - 1) * 250, () => {
        readAhead(key, (signal) => readPath(key, signal, true), true);
        active--;
        pump();
      });
    }
  };
  pump();
}

async function durableUsedKeys(): Promise<string[]> {
  const st = desktopStore();
  if (!st) return [];
  try {
    if (typeof st.usedKeys === "function") return await st.usedKeys();
    if (cachedUsedKeys !== null) return cachedUsedKeys;
    cachedUsedKeys = (await st.open()).usedKeys ?? [];
    return cachedUsedKeys;
  } catch {
    return [];
  }
}

export function refreshStoredReads(spread = false): void {
  rereadActive();
  if (viewingAs()) return;
  const skip = new Set(activeReadKeys());
  const memory = keptKeys().reverse();   // keptKeys() is oldest-first; catch up most-recent-first
  scheduleKeys(memory, spread, skip);
  // the desktop's durable tier: everything used in the last hour catches up too, even when
  // it is not in this tab's memory yet; an older shell has no usedKeys and simply adds none
  void durableUsedKeys().then((keys) => scheduleKeys(keys, spread, skip));
}

/** One decoded event. Exported so the stream's exact handling is testable. */
export function handlePageEvent(name: string, data: unknown): void {
  const body = (data ?? {}) as Record<string, unknown>;
  if (name === "cycle" || name === "mine") {
    refreshStoredReads(false);
    return;
  }
  if (name === "door" && typeof body.door === "string") {
    // The same fail-closed door rule as every API answer: a moved door wipes before use.
    noteAnswerHeaders(new Headers({ "X-PLX-Door": body.door }));
    rereadActive();
    return;
  }
  if (name === "release" && typeof body.release === "string") {
    if (releaseSeen === null) releaseSeen = body.release;
    else if (releaseSeen !== body.release) scheduleReload();
  }
}

async function oneStream(onHealthy: () => void): Promise<void> {
  const ctl = new AbortController();
  current = ctl;
  const response = await fetch("/api/events", {
    credentials: "same-origin",
    headers: await ownerStreamHeaders({ Accept: "text/event-stream" }),
    signal: ctl.signal,
  });
  noteAnswerHeaders(response.headers);
  if (response.status === 401 || response.headers.get("X-PLX-Session") === "refused") {
    stopEvents();
    return;
  }
  if (!response.ok || !response.body) throw new Error(`events ${response.status}`);
  onHealthy();
  if (missed) {
    missed = false;
    refreshStoredReads(true);   // a network/server gap: catch up over a minute
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  const parser = new SseParser();
  for (;;) {
    let stall: ReturnType<typeof setTimeout> | undefined;
    const chunk = await Promise.race([
      reader.read(),
      new Promise<never>((_, fail) => {
        stall = setTimeout(() => fail(new Error("events stalled")), STREAM_STALL_MS);
      }),
    ]).finally(() => clearTimeout(stall));
    const { value, done } = chunk;
    if (done) break;
    onHealthy();
    for (const event of parser.push(decoder.decode(value, { stream: true }))) {
      try {
        handlePageEvent(event.event, JSON.parse(event.data));
      } catch (e) {
        console.warn("events: bad event", e);
      }
    }
  }
  missed = true;   // a clean end is still a lost push channel: the next connect catches up
}

async function loop(): Promise<void> {
  let backoff = FIRST_BACKOFF_MS;
  while (!stopped) {
    try {
      await oneStream(() => {
        backoff = FIRST_BACKOFF_MS;
      });
    } catch {
      missed = true;
    } finally {
      current = null;
    }
    if (stopped) break;
    await wait(backoff);
    backoff = Math.min(MOST_BACKOFF_MS, backoff * 2);
  }
  running = false;
}

/** Start the session's one stream (idempotent). */
export function startEvents(): void {
  if (running || typeof window === "undefined") return;
  stopped = false;
  running = true;
  void loop();
}

/** End it (sign-out/session reset); no stream or catch-up read outlives the session. */
export function stopEvents(): void {
  stopped = true;
  current?.abort();
  for (const t of timers) clearTimeout(t);
  timers.clear();
}

onSessionReset(() => {
  cachedUsedKeys = null;
  stopEvents();
});

/** Tests start each case from nothing. */
export function resetEventsForTests(): void {
  stopEvents();
  running = false;
  missed = false;
  releaseSeen = null;
  cachedUsedKeys = null;
  reloadPending = false;
}
