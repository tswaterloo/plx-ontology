/** The reader's did-you-mean picks for this browser session: the same choices again
 * offer the last pick first ("last picked"), never pick it for them. */
const KEY = "plx.picks";

function choicesKey(ids: string[]): string {
  return [...ids].sort().join("\u0000");
}

function read(): Record<string, string> {
  try {
    const v = JSON.parse(sessionStorage.getItem(KEY) ?? "{}");
    return v && typeof v === "object" ? (v as Record<string, string>) : {};
  } catch {
    return {};
  }
}

export function lastPick(ids: string[]): string | undefined {
  if (typeof window === "undefined" || ids.length < 2) return undefined;
  const id = read()[choicesKey(ids)];
  return id && ids.includes(id) ? id : undefined;
}

export function rememberPick(ids: string[], picked: string): void {
  if (typeof window === "undefined" || ids.length < 2) return;
  const all = read();
  all[choicesKey(ids)] = picked;
  try {
    sessionStorage.setItem(KEY, JSON.stringify(all));
  } catch {
    // storage full or off: the pick is just not remembered
  }
}
