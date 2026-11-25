/** What a tab keeps while the person is away (an unsent question, the last answer on
 * Insights), in sessionStorage: per tab, gone when the tab closes. Storage that is off,
 * full or holds something else only means nothing is remembered; the page works the same. */
export function recall<T>(key: string, ok: (v: unknown) => v is T): T | undefined {
  try {
    const raw = sessionStorage.getItem(key);
    if (!raw) return undefined;
    const v: unknown = JSON.parse(raw);
    return ok(v) ? v : undefined;
  } catch {
    return undefined;
  }
}

export function remember(key: string, value: unknown): void {
  try {
    sessionStorage.setItem(key, JSON.stringify(value));
  } catch {
    // storage off or full: not remembered
  }
}

export function forget(key: string): void {
  try {
    sessionStorage.removeItem(key);
  } catch {
    // storage off: nothing to forget
  }
}
