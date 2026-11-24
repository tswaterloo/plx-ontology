import { useEffect, useState } from "react";

/** How long after a page served stale it is first asked for again, and how often at most.
 * Each later try waits twice as long (1.5, 3, 6, 12 s: ~22 s in all, as four tries 6 s
 * apart were). The server starts reading the page again the moment it serves it stale, and
 * the read is usually done within 1-2 s (live 11-22): a fixed 6 s showed the old page ~4 s
 * longer than needed. A try that comes too early costs one small request (the server
 * answers from the kept page while its read goes on). */
export const STALE_REFRESH_MS = 1500;
export const STALE_REFRESH_TRIES = 4;
/** The wait before try ``tries`` + 1. */
export const staleRefreshDelay = (tries: number) => STALE_REFRESH_MS * 2 ** tries;

/**
 * A page the server served from the reader's last read while it reads it again
 * ("As of 9:08 PM ET · refreshing") is asked for again a few seconds later, in place, so
 * the fresh page replaces it without a reload; a few tries at most per visit.
 */
export function useStaleRefresh(staleSince: string | null | undefined, refresh: () => void) {
  // counted in state, so each try schedules the next even when the server keeps sending the
  // same stale_since; a fresh answer (no stale_since) ends the episode and resets the budget
  const [tries, setTries] = useState(0);
  useEffect(() => {
    if (!staleSince) {
      if (tries) setTries(0);
      return;
    }
    if (tries >= STALE_REFRESH_TRIES) return;
    const t = setTimeout(() => {
      setTries((n) => n + 1);
      refresh();
    }, staleRefreshDelay(tries));
    return () => clearTimeout(t);
  }, [staleSince, refresh, tries]);
}

/**
 * The same, but asked when the person comes back to the window (it gains focus or becomes
 * visible) instead of on a timer from first paint: a page's own record and Home no longer send a
 * second read of what they just read while it is still being drawn. The tries budget is the same;
 * two events within a second count once. Pair the page's `StaleNote` with `onReturn`, so it says
 * so rather than "refreshing".
 */
export function useStaleRefreshOnReturn(staleSince: string | null | undefined, refresh: () => void) {
  const [tries, setTries] = useState(0);
  useEffect(() => {
    if (!staleSince) {
      if (tries) setTries(0);
      return;
    }
    if (tries >= STALE_REFRESH_TRIES) return;
    let last = 0;
    const back = () => {
      if (typeof document !== "undefined" && document.visibilityState === "hidden") return;
      const now = Date.now();
      if (now - last < 1000) return;
      last = now;
      setTries((n) => n + 1);
      refresh();
    };
    window.addEventListener("focus", back);
    document.addEventListener("visibilitychange", back);
    return () => {
      window.removeEventListener("focus", back);
      document.removeEventListener("visibilitychange", back);
    };
  }, [staleSince, refresh, tries]);
}
