import { useEffect, useRef } from "react";

/** How long after a page served stale it is asked for again, and how often at most. */
export const STALE_REFRESH_MS = 6000;
export const STALE_REFRESH_TRIES = 4;

/**
 * A page the server served from the reader's last read while it reads it again
 * ("As of 9:08 PM ET · refreshing") is asked for again a few seconds later, in place, so
 * the fresh page replaces it without a reload; a few tries at most per visit.
 */
export function useStaleRefresh(staleSince: string | null | undefined, refresh: () => void) {
  const tries = useRef(0);
  useEffect(() => {
    if (!staleSince || tries.current >= STALE_REFRESH_TRIES) return;
    const t = setTimeout(() => {
      tries.current += 1;
      refresh();
    }, STALE_REFRESH_MS);
    return () => clearTimeout(t);
  }, [staleSince, refresh]);
}
