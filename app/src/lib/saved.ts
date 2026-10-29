"use client";

// Saved items live in this browser (localStorage), one list per signed-in
// person, and that person's list is deleted on sign-out, so a shared device
// never shows one person's saved questions to the next. With nobody signed in
// there is no list and nothing can be saved.

import { useSyncExternalStore } from "react";

export interface SavedItem {
  key: string; // "q:<question>" or "o:<type>/<id>"
  kind: "question" | "object";
  label: string;
  href: string;
  at: string;
}

const PREFIX = "plx.saved.v2:";
const LEGACY = "plx.saved.v1"; // pre-per-user list: removed on sight
const listeners = new Set<() => void>();
const EMPTY: SavedItem[] = [];
let user: string | null = null;
let cache: SavedItem[] | null = null;

function storageKey(u: string): string {
  return PREFIX + u;
}

function emit() {
  listeners.forEach((l) => l());
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

/** Sign-out: delete this person's saved list from the browser. */
export function clearSavedFor(principal: string | null) {
  if (principal) {
    try {
      window.localStorage.removeItem(storageKey(principal));
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
  cache = items.slice(0, 50);
  try {
    window.localStorage.setItem(storageKey(user), JSON.stringify(cache));
  } catch {
    /* storage full or blocked: keep in memory */
  }
  emit();
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
export function toggleSaved(item: Omit<SavedItem, "at">): boolean {
  if (!user) return false;
  const items = read();
  if (items.some((s) => s.key === item.key)) {
    write(items.filter((s) => s.key !== item.key));
    return false;
  }
  write([{ ...item, at: new Date().toISOString() }, ...items]);
  return true;
}
