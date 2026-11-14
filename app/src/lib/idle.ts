/** Runs `fn` once the browser is idle (soon after first paint); returns a cancel. */
export function whenIdle(fn: () => unknown): () => void {
  if (typeof window === "undefined") return () => undefined;
  const w = window as Window & { requestIdleCallback?: (cb: () => void, o?: { timeout: number }) => number; cancelIdleCallback?: (h: number) => void };
  if (w.requestIdleCallback) {
    const h = w.requestIdleCallback(() => void fn(), { timeout: 3000 });
    return () => w.cancelIdleCallback?.(h);
  }
  const t = window.setTimeout(() => void fn(), 1500);
  return () => window.clearTimeout(t);
}
