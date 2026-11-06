"use client";

import { useEffect, useState } from "react";

/** A Mac (or iPhone/iPad) reader: shortcuts are shown with ⌘; everyone else (Windows, Linux) sees Ctrl. */
export function isApple(): boolean {
  if (typeof navigator === "undefined") return false;
  return /Macintosh|Mac OS X|iPhone|iPad|iPod/.test(navigator.userAgent);
}

export function isWindows(): boolean {
  if (typeof navigator === "undefined") return false;
  return /Windows/.test(navigator.userAgent);
}

/**
 * The modifier as this reader's keyboard says it: "⌘" on a Mac, "Ctrl" elsewhere. Drawn after
 * mount (the server can't know), as Ctrl until then. `shortcut("K")` is "⌘K" or "Ctrl K".
 */
export function useShortcut(): { mod: string; shortcut: (key: string) => string } {
  const [apple, setApple] = useState(false);
  useEffect(() => setApple(isApple()), []);
  const mod = apple ? "⌘" : "Ctrl";
  return { mod, shortcut: (key: string) => (apple ? `⌘${key}` : `Ctrl ${key}`) };
}
