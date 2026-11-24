import type { LatePo, ObjectRef, ProactiveSuggestion, SourceSystem } from "@/lib/types";

/** Days-late bands of the late list's filter. */
export const DAY_BANDS = [
  { id: "all", label: "All", test: () => true },
  { id: "d30", label: "Up to 30 days", test: (d: number) => d <= 30 },
  { id: "d90", label: "31 to 90 days", test: (d: number) => d > 30 && d <= 90 },
  { id: "dmore", label: "Over 90 days", test: (d: number) => d > 90 },
] as const;

/** Value bands, in each order's own currency (no conversion between currencies). */
export const VALUE_BANDS = [
  { id: "all", label: "Any value" },
  { id: "v1k", label: "Under 1,000", lo: 0, hi: 1_000 },
  { id: "v10k", label: "1,000 to 10,000", lo: 1_000, hi: 10_000 },
  { id: "v100k", label: "10,000 to 100,000", lo: 10_000, hi: 100_000 },
  { id: "vmore", label: "Over 100,000", lo: 100_000, hi: Infinity },
  { id: "none", label: "No value shown" },
] as const;

export type DayBand = (typeof DAY_BANDS)[number]["id"];
export type ValueBand = (typeof VALUE_BANDS)[number]["id"];
export type SortKey = "days" | "value" | "supplier" | "due" | "po";
/** What blocks a late order, by its supplier date state (the buyer's filter). */
export const STATE_BANDS = [
  { id: "all", label: "Any state" },
  { id: "no_date", label: "No supplier date", test: (p: LatePo) => p.confirmation === "none_found" },
  { id: "check", label: "Date to check", test: (p: LatePo) => p.confirmation === "found_unverified" || p.confirmation === "needs_check" },
  { id: "confirmed", label: "Supplier confirmed", test: (p: LatePo) => p.confirmation === "confirmed" },
  { id: "unknown", label: "State not on the list", test: (p: LatePo) => !p.confirmation },
] as const;
export type StateBand = (typeof STATE_BANDS)[number]["id"];

export interface LateFilter {
  supplier: string | null; // null: every supplier; "": orders with no supplier shown
  days: DayBand;
  value: ValueBand;
  state?: StateBand;
}

export function inStateBand(p: LatePo, band: StateBand | undefined): boolean {
  if (!band || band === "all") return true;
  const b = STATE_BANDS.find((x) => x.id === band);
  return !!b && "test" in b && b.test(p); // an unknown band shows nothing
}

/** An order's total as a number for banding and sorting, or null: none shown, or suspect (never treated as fact). */
export function valueOf(p: LatePo): number | null {
  if (!p.amount || p.amount_suspect) return null;
  const n = Number(p.amount.amount);
  return Number.isFinite(n) ? n : null;
}

export function inValueBand(p: LatePo, band: ValueBand): boolean {
  if (band === "all") return true;
  const v = valueOf(p);
  if (band === "none") return v === null;
  const b = VALUE_BANDS.find((x) => x.id === band);
  return v !== null && !!b && "lo" in b && v >= b.lo && v < b.hi;
}

export function inDayBand(p: LatePo, band: DayBand): boolean {
  const b = DAY_BANDS.find((x) => x.id === band);
  if (!b) return false; // an unknown band shows nothing
  return band === "all" || (typeof p.days_late === "number" && b.test(p.days_late));
}

export function matches(p: LatePo, f: LateFilter): boolean {
  return (f.supplier === null || (p.supplier ?? "") === f.supplier) && inDayBand(p, f.days) && inValueBand(p, f.value) && inStateBand(p, f.state);
}

/** Every supplier on the list with its count of late orders, most first ("" for none shown). */
export function supplierCounts(items: LatePo[]): [string, number][] {
  const m = new Map<string, number>();
  for (const p of items) m.set(p.supplier ?? "", (m.get(p.supplier ?? "") ?? 0) + 1);
  return [...m.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
}

/**
 * Sorted copy. Days late and value: largest first; supplier, PO and due date: A to Z /
 * earliest first; `desc` flips. Value sorts within each currency (codes A to Z), never
 * across them; an order without a value shown sorts last either way, as does a missing
 * supplier, due date or days late.
 */
export function sortLate(items: LatePo[], key: SortKey, desc = false): LatePo[] {
  const flip = desc ? -1 : 1;
  const last = <T,>(a: T | null | undefined, b: T | null | undefined, cmp: (x: T, y: T) => number) =>
    a == null && b == null ? 0 : a == null ? 1 : b == null ? -1 : flip * cmp(a, b);
  const cmp: Record<SortKey, (a: LatePo, b: LatePo) => number> = {
    days: (a, b) => last(a.days_late, b.days_late, (x, y) => y - x),
    value: (a, b) => {
      const va = valueOf(a), vb = valueOf(b);
      if (va === null || vb === null) return last(va, vb, () => 0);
      const ca = a.amount!.currency, cb = b.amount!.currency;
      return ca !== cb ? ca.localeCompare(cb) : flip * (vb - va);
    },
    supplier: (a, b) => last(a.supplier || null, b.supplier || null, (x, y) => x.localeCompare(y)),
    due: (a, b) => last(a.due_date, b.due_date, (x, y) => x.localeCompare(y)),
    po: (a, b) => flip * a.label.localeCompare(b.label, undefined, { numeric: true }),
  };
  return items
    .map((p, i) => [p, i] as const)
    .sort(([a, i], [b, j]) => cmp[key](a, b) || i - j)
    .map(([p]) => p);
}

/** One page of the late list as `/api/late` returns it (the fields `readAllLate` reads). */
export interface LatePage {
  items?: LatePo[] | null;
  total: number;
  more?: boolean;
  next_offset?: number | null;
  as_of?: string | null;
  stale_since?: string | null;
  stale_total?: number | null;
}

/**
 * Every late PO up to `most`, in the list's order, each once: the first page, then every
 * further page at once (a page ends where the next starts), not one round trip after another.
 * Returns the orders, the list's total and the cut-off the last page states.
 */
export async function readAllLate<P extends LatePage>(
  get: (offset: number, size: number) => Promise<P>,
  size: number,
  most: number,
): Promise<{ items: LatePo[]; total: number; asOf: string | null; staleSince: string | null; staleTotal: number | null }> {
  const first = await get(0, size);
  const pages: P[] = [first];
  const step = first.next_offset ?? (first.items ?? []).length;
  if (first.more && step > 0) {
    const offsets: number[] = [];
    for (let o = step; o < Math.min(first.total, most); o += step) offsets.push(o);
    pages.push(...(await Promise.all(offsets.map((o) => get(o, size)))));
  }
  const items: LatePo[] = [];
  const seen = new Set<string>();
  for (const page of pages)
    for (const p of page.items ?? []) if (!seen.has(p.object_id)) (seen.add(p.object_id), items.push(p));
  const last = pages[pages.length - 1];
  const staleSince = pages.map((p) => p.stale_since).filter((t): t is string => !!t).sort()[0] ?? null;
  return { items, total: last.total, asOf: last.as_of ?? null, staleSince, staleTotal: first.stale_total ?? null };
}

export const DEC = /^-?\d+(\.\d+)?$/;

/** a + b for two decimal strings, exactly (no float on the way: money stays to the cent). */
export function addDecimal(a: string, b: string): string {
  const scale = Math.max((a.split(".")[1] ?? "").length, (b.split(".")[1] ?? "").length);
  const big = (s: string) => {
    const neg = s.startsWith("-");
    const [i, f = ""] = (neg ? s.slice(1) : s).split(".");
    const n = BigInt(i + f.padEnd(scale, "0"));
    return neg ? -n : n;
  };
  const sum = big(a) + big(b);
  const neg = sum < BigInt(0);
  const digits = (neg ? -sum : sum).toString().padStart(scale + 1, "0");
  const out = scale ? `${digits.slice(0, -scale)}.${digits.slice(-scale)}` : digits;
  return neg ? `-${out}` : out;
}

/** Each currency's exact total of the late POs that carry a trusted value, largest first; amounts in different currencies are never added together. */
export function lateTotals(items: LatePo[]): { currency: string; amount: string; count: number }[] {
  const m = new Map<string, { amount: string; count: number }>();
  for (const p of items) {
    if (!p.amount || p.amount_suspect) continue;
    const a = String(p.amount.amount).trim();
    if (!DEC.test(a)) continue;
    const cur = m.get(p.amount.currency);
    m.set(p.amount.currency, { amount: cur ? addDecimal(cur.amount, a) : a, count: (cur?.count ?? 0) + 1 });
  }
  return [...m.entries()].map(([currency, v]) => ({ currency, ...v })).sort((x, y) => Number(y.amount) - Number(x.amount));
}

/** What a buyer reads to chase one late order, from its drafted follow-up: the date the
 * supplier gave (or why there is none), the last word on record and where, whom to ask,
 * the customer or production orders it holds up and the next step. Null fields: not on
 * record (said in words by the row, never guessed). */
export interface Chase {
  promised: string | null; // "4 Nov" style is the caller's; this is the ISO date or day
  promiseWords: string | null; // when there is no settled date: what the record says instead
  lastWord: { at: string; system: SourceSystem } | null;
  ask: string | null;
  holds: ObjectRef[];
  next: string | null;
  /** The rule behind ``next`` (late_ask_supplier...): the row says it in its own words. */
  rule?: string | null;
}

const ETA_WORDS: Record<string, string | null> = { no: "supplier gave no date", possibly: "possible date, unconfirmed", conflicting: "dates on record disagree", hidden: "date in a record you can't open", unknown: null, yes: null };

export function chaseOf(s: ProactiveSuggestion | null | undefined): Chase {
  const eta = s?.eta ?? null;
  const promised = eta && (eta.answer === "yes" || eta.answer === "possibly") && eta.when ? eta.when : null;
  const at = eta?.source?.as_of ?? null;
  const to = s?.draft?.to;
  return {
    promised,
    promiseWords: eta ? (eta.answer === "possibly" && promised ? ETA_WORDS.possibly : promised ? null : (ETA_WORDS[eta.answer] ?? null)) : null,
    lastWord: at && eta?.source ? { at, system: eta.source.system } : null,
    ask: to?.name || to?.address || s?.action?.contact || null,
    holds: s?.impact?.orders ?? [],
    next: s?.action?.text ?? null,
    rule: s?.action?.rule ?? null,
  };
}
