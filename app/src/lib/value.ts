/**
 * The one envelope every served number, list and feed arrives in (contract C1), the same as
 * `operating/value.py`: `{value, status, total, as_of, why, source_job}`. A list's `value` is
 * `{items, total, next}`. Anything not `ok` is said by `StatusLine`; a `cut` list is rendered by
 * `CutList`, which shows the exact total and reaches the rest through `next`.
 *
 * A status outside `STATUSES` is refused (`parseValue` throws): an envelope the app cannot
 * render is never shown as fine, and a missing number is never a bare 0.
 */
import { eastern } from "@/lib/format";

export const STATUSES = ["ok", "partial", "cut", "stale", "unknown", "not_run", "off", "failed"] as const;
export type Status = (typeof STATUSES)[number];

export type Value<T = unknown> = {
  value: T | null;
  status: Status;
  total: number | null;
  as_of: string | null;
  why: string | null;
  source_job: string | null;
};

export type Listed<T> = { items: T[]; total: number; next: string | number | null };

const FIELDS = ["value", "status", "total", "as_of", "why", "source_job"] as const;

export function isStatus(s: unknown): s is Status {
  return typeof s === "string" && (STATUSES as readonly string[]).includes(s);
}

/** An envelope from the server, every field checked; throws on an unknown status or field. */
export function parseValue<T = unknown>(raw: unknown): Value<T> {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) throw new Error("not a value envelope");
  const r = raw as Record<string, unknown>;
  const extra = Object.keys(r).filter((k) => !(FIELDS as readonly string[]).includes(k));
  if (extra.length) throw new Error(`unknown envelope fields: ${extra.join(", ")}`);
  if (!isStatus(r.status)) throw new Error(`unknown status: ${String(r.status)}`);
  if (r.status !== "ok" && !r.why && !r.as_of) throw new Error(`status ${r.status} without why or as_of`);
  const total = r.total ?? null;
  if (total !== null && (typeof total !== "number" || !Number.isInteger(total) || total < 0)) throw new Error("total must be a count");
  const text = (k: string) => (typeof r[k] === "string" ? (r[k] as string) : null);
  return { value: (r.value ?? null) as T | null, status: r.status, total, as_of: text("as_of"), why: text("why"), source_job: text("source_job") };
}

/** The sentence a status line says for `v`, or null when it is `ok` (nothing to say). */
export function statusText(v: Pick<Value, "status" | "as_of" | "why">, now: Date = new Date()): string | null {
  const when = v.as_of ? eastern(v.as_of, now) : "";
  const why = v.why?.trim() || "";
  switch (v.status) {
    case "ok":
      return null;
    case "stale":
      return [when ? `As of ${when}` : "Older than its source", why].filter(Boolean).join(": ");
    case "off":
      return why ? `Switched off: ${why}` : "Switched off";
    case "not_run":
      return why ? `Not run yet: ${why}` : "Not run yet";
    case "unknown":
      return why ? `Not recorded: ${why}` : "Not recorded";
    case "failed":
      return why ? `Reading failed: ${why}` : "Reading failed";
    case "partial":
      return why ? `Partly read: ${why}` : "Partly read";
    case "cut":
      return why || "Shortened";
  }
}
