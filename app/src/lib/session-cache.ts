// Reads the browser keeps for the page's life (Today's /api/home, the late list) belong to
// the person signed in when they were read. Each cache registers how to drop itself; the
// auth layer drops them all when that person signs out or another signs in, so nothing read
// for one person is ever shown to the next in the same tab.

const resets = new Set<() => void>();

/** Register how a module-level cache forgets what it holds. */
export function onSessionReset(reset: () => void): () => void {
  resets.add(reset);
  return () => resets.delete(reset);
}

/** Drop every registered cache (sign-out, or a different person signed in). */
export function resetSessionCaches(): void {
  for (const reset of resets) reset();
}
