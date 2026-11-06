import type { LatePo } from "@/lib/types";

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
export interface LateFilter {
  supplier: string | null; // null: every supplier; "": orders with no supplier shown
  days: DayBand;
  value: ValueBand;
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
  return (f.supplier === null || (p.supplier ?? "") === f.supplier) && inDayBand(p, f.days) && inValueBand(p, f.value);
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
): Promise<{ items: LatePo[]; total: number; asOf: string | null }> {
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
  return { items, total: last.total, asOf: last.as_of ?? null };
}
