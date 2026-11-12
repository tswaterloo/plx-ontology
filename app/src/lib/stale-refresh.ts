import { useEffect, useState } from "react";

/** How long after a page served stale it is asked for again, and how often at most. */
export const STALE_REFRESH_MS = 6000;
export const STALE_REFRESH_TRIES = 4;

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
    }, STALE_REFRESH_MS);
    return () => clearTimeout(t);
  }, [staleSince, refresh, tries]);
}
