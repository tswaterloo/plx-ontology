"use client";

// Saved items live in this browser (localStorage), one list per signed-in
// person, and that person's list is deleted on sign-out, so a shared device
// never shows one person's saved questions to the next. With nobody signed in
// there is no list and nothing can be saved. Each Save and Unsave is also kept
// in the person's work log on the server (best effort: the browser list stands).

import { useSyncExternalStore } from "react";
import { logSaved } from "./api";

export interface SavedItem {
  key: string; // "q:<question>", "o:<type>/<id>" or "p:<page href>" (a pinned page, lib/pins)
  kind: "question" | "object" | "page";
  label: string;
  href: string;
  at: string;
}

const PREFIX = "plx.saved.v2:";
export const RECENT_PREFIX = "plx.recent.v1:";
const LEGACY = "plx.saved.v1"; // pre-per-user list: removed on sight
const listeners = new Set<() => void>();
const EMPTY: SavedItem[] = [];
let user: string | null = null;
let cache: SavedItem[] | null = null;

function storageKey(u: string): string {
  return PREFIX + u;
}

function emit() {
  recentCache = null;
  listeners.forEach((l) => l());
}

// ---------------------------------------------------------------- the records opened lately
// Kept like the saved list: in this browser, per signed-in person, deleted on sign-out; the
// Records tabs list them so a tab is never only a search box.

export interface OpenedRecord {
  type: string;
  label: string;
  href: string;
  at: string;
}
const RECENT_MAX = 30;
const EMPTY_RECENT: OpenedRecord[] = [];
let recentCache: OpenedRecord[] | null = null;

function readRecent(): OpenedRecord[] {
  if (!user) return EMPTY_RECENT;
  if (recentCache) return recentCache;
  try {
    const parsed = JSON.parse(window.localStorage.getItem(RECENT_PREFIX + user) ?? "[]") as unknown;
    recentCache = Array.isArray(parsed)
      ? parsed.filter((x): x is OpenedRecord => !!x && typeof x.type === "string" && typeof x.label === "string" && typeof x.href === "string" && x.href.startsWith("/") && !x.href.startsWith("//"))
      : [];
  } catch {
    recentCache = [];
  }
  return recentCache;
}

/** A record page was opened: it goes to the top of this person's recent list (once). */
export function rememberOpened(r: Omit<OpenedRecord, "at">): void {
  if (!user || !r.label.trim() || !r.href.startsWith("/") || r.href.startsWith("//")) return;
  const next = [{ ...r, at: new Date().toISOString() }, ...readRecent().filter((x) => x.href !== r.href)].slice(0, RECENT_MAX);
  try {
    window.localStorage.setItem(RECENT_PREFIX + user, JSON.stringify(next));
  } catch {
    /* storage full or blocked */
  }
  recentCache = next;
  listeners.forEach((l) => l());
}

export function useRecentRecords(): OpenedRecord[] {
  return useSyncExternalStore(
    (l) => {
      listeners.add(l);
      return () => listeners.delete(l);
    },
    readRecent,
    () => EMPTY_RECENT,
  );
}

/** Called by the auth layer: the signed-in principal, or null when signed out. */
export function setSavedUser(principal: string | null) {
  if (principal === user) return;
  user = principal;
  cache = null;
  try {
    window.localStorage.removeItem(LEGACY);
  } catch {
    /* storage blocked */
  }
  emit();
}

/** The person whose lists this browser holds now (null: nobody signed in). */
export function savedUser(): string | null {
  return user;
}

/** Sign-out: delete this person's saved list (and the records they opened) from the browser. */
export function clearSavedFor(principal: string | null) {
  if (principal) {
    try {
      window.localStorage.removeItem(storageKey(principal));
      window.localStorage.removeItem(RECENT_PREFIX + principal);
    } catch {
      /* storage blocked */
    }
  }
  if (principal === user) {
    user = null;
    cache = null;
    emit();
  }
}

function read(): SavedItem[] {
  if (!user) return EMPTY;
  if (cache) return cache;
  try {
    const raw = window.localStorage.getItem(storageKey(user));
    const parsed = raw ? (JSON.parse(raw) as unknown) : [];
    cache = Array.isArray(parsed)
      ? parsed.filter(
          (x): x is SavedItem =>
            !!x &&
            typeof x.key === "string" &&
            typeof x.label === "string" &&
            typeof x.href === "string" &&
            x.href.startsWith("/") &&
            !x.href.startsWith("//"),
        )
      : [];
  } catch {
    cache = [];
  }
  return cache;
}

function write(items: SavedItem[]) {
  if (!user) return;
  // up to 50 saved questions and records, plus the pinned pages (lib/pins caps those at 20)
  const pages = items.filter((s) => s.kind === "page").slice(0, 21);
  cache = [...pages, ...items.filter((s) => s.kind !== "page").slice(0, 50)];
  try {
    window.localStorage.setItem(storageKey(user), JSON.stringify(cache));
  } catch {
    /* storage full or blocked: keep in memory */
  }
  emit();
}

/** Replace this person's pinned pages (kind "page"; lib/pins) and keep everything else.
 * False when nobody is signed in. Pins are kept in the browser only, not the work log. */
export function writePages(pages: SavedItem[]): boolean {
  if (!user) return false;
  const rest = read().filter((s) => s.kind !== "page");
  write([...pages.filter((p) => p.kind === "page"), ...rest.slice(0, 50)]);
  return true;
}

export function useSaved(): SavedItem[] {
  return useSyncExternalStore(
    (l) => {
      listeners.add(l);
      return () => listeners.delete(l);
    },
    read,
    () => EMPTY,
  );
}

/** Returns whether the item is now saved. No-op (false) when nobody is signed in. */
export function toggleSaved(item: Omit<SavedItem, "at"> & { kind: "question" | "object" }): boolean {
  if (!user) return false;
  const items = read();
  const saved = !items.some((s) => s.key === item.key);
  write(saved ? [{ ...item, at: new Date().toISOString() }, ...items] : items.filter((s) => s.key !== item.key));
  // "o:<type>/<id>": a record; "q:<question>": a question
  const record = item.kind === "object" ? /^o:([^/]+)\/(.+)$/.exec(item.key) : null;
  void logSaved({
    saved,
    kind: item.kind,
    label: item.label.slice(0, 300) || "Saved",
    ...(record ? { object_type: record[1], object_id: record[2] } : {}),
  }).catch(() => undefined);
  return saved;
}
