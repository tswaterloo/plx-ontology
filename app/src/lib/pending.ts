"use client";

// Hands the picked autocomplete entities from the Home ask box to /ask
// (the URL carries only the question text).

import type { Pin } from "@/components/ask-box";

const KEY = "plx.pending.v1";

export function stashPending(question: string, pins: Pin[]) {
  try {
    sessionStorage.setItem(KEY, JSON.stringify({ question, pins }));
  } catch {
    /* private mode: pins are a hint only */
  }
}

export function takePending(question: string): Pin[] {
  try {
    const raw = sessionStorage.getItem(KEY);
    sessionStorage.removeItem(KEY);
    if (!raw) return [];
    const v = JSON.parse(raw) as { question?: unknown; pins?: unknown };
    if (v.question !== question || !Array.isArray(v.pins)) return [];
    return v.pins.filter(
      (p): p is Pin => !!p && typeof p.object_id === "string" && typeof p.type === "string" && typeof p.label === "string",
    );
  } catch {
    return [];
  }
}

export function clearPending() {
  try {
    sessionStorage.removeItem(KEY);
  } catch {
    /* blocked */
  }
}
