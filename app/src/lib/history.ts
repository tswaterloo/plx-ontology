// One stream of what happened on a record, across systems: the record's timeline events
// and its linked mail, Teams messages, files and tasks, newest first, each with the system
// it came from and a kind (orders, shipments, invoices, payments, mail, Teams, meetings,
// tasks). Only what the server sent for this reader; nothing is counted that was not sent.

import type { LinkedItem, Source, SourceSystem, TimelineEvent } from "./types";

export const HISTORY_KINDS = ["Orders", "Shipments", "Invoices", "Payments", "Mail", "Teams", "Meetings", "Tasks", "Files", "Other"] as const;
export type HistoryKind = (typeof HISTORY_KINDS)[number];

export type HistoryRow = {
  at: string | null;
  system: SourceSystem;
  kind: HistoryKind;
  title: string;
  summary: string | null;
  source: Source | null;
  /** The same title on the same day from the same system, said once. */
  count: number;
};

const WORDS: [RegExp, HistoryKind][] = [
  [/\b(meeting|call|transcript|minutes)\b/i, "Meetings"],
  [/\b(payment|paid|remittance|deposit)\b/i, "Payments"],
  [/\b(invoice|invoiced|billed|credit memo)\b/i, "Invoices"],
  [/\b(shipment|shipped|ship|delivered|delivery|dispatch|received|receipt|tracking)\b/i, "Shipments"],
  [/\b(task|to-?do|follow[- ]?up)\b/i, "Tasks"],
  [/\b(order|released|quote|confirmed)\b|\bPO\b|\bSO\b/i, "Orders"],
];

/** The kind of a timeline event: its system first for mail and Teams, else its words. */
export function kindOf(system: SourceSystem, title: string): HistoryKind {
  if (system === "Email") return "Mail";
  if (system === "Teams") return /\bmeeting\b/i.test(title) ? "Meetings" : "Teams";
  for (const [re, kind] of WORDS) if (re.test(title)) return kind;
  return system === "File" ? "Files" : "Other";
}

const LINK_KIND: Record<LinkedItem["kind"], HistoryKind | null> = { email: "Mail", teams: "Teams", document: "Files", task: "Tasks", record: null };
const LINK_SYSTEM: Record<LinkedItem["kind"], SourceSystem> = { email: "Email", teams: "Teams", document: "File", task: "Portal", record: "BC" };

export function buildHistory(events: TimelineEvent[], linked: LinkedItem[]): HistoryRow[] {
  const rows: HistoryRow[] = [];
  const day = (at: string | null) => (at ?? "").slice(0, 10);
  // timeline events are each their own row, however many share a source and day (an order
  // released at 10:00 and received at 11:00 are two things that happened)
  const eventDays = new Set<string>();
  for (const e of events) {
    if (e.source) eventDays.add(`${e.source.handle}|${day(e.at ?? null)}`);
    rows.push({ at: e.at ?? null, system: e.system, kind: kindOf(e.system, e.title), title: e.title, summary: e.summary ?? null, source: e.source ?? null, count: 1 });
  }
  // a linked item that is the same source on the same day as a timeline event is that event;
  // two linked items collapse only when they are the same source at the same moment
  const linkedSeen = new Set<string>();
  for (const l of linked) {
    const at = l.at ?? null;
    if (l.source) {
      if (eventDays.has(`${l.source.handle}|${day(at)}`)) continue;
      const k = `${l.source.handle}|${at ?? ""}`;
      if (linkedSeen.has(k)) continue;
      linkedSeen.add(k);
    }
    rows.push({ at, system: l.source?.system ?? LINK_SYSTEM[l.kind], kind: LINK_KIND[l.kind] ?? kindOf(l.source?.system ?? LINK_SYSTEM[l.kind], l.label), title: l.label, summary: null, source: l.source ?? null, count: 1 });
  }
  // dated newest first, then the undated; stable within a day
  rows.sort((a, b) => (a.at && b.at ? b.at.localeCompare(a.at) : a.at ? -1 : b.at ? 1 : 0));
  const out: HistoryRow[] = [];
  for (const r of rows) {
    const prev = out[out.length - 1];
    if (prev && prev.title === r.title && prev.system === r.system && prev.kind === r.kind && (prev.at ?? "").slice(0, 10) === (r.at ?? "").slice(0, 10) && !r.source && !prev.source) prev.count += 1;
    else out.push(r);
  }
  return out;
}

export type HistoryFilter = { systems: ReadonlySet<SourceSystem>; kinds: ReadonlySet<HistoryKind> };

/** An empty set filters nothing. */
export function filterHistory(rows: HistoryRow[], f: HistoryFilter): HistoryRow[] {
  return rows.filter((r) => (f.systems.size === 0 || f.systems.has(r.system)) && (f.kinds.size === 0 || f.kinds.has(r.kind)));
}

/** The systems and kinds present in the rows, in a fixed order, for the filter chips. */
export function facets(rows: HistoryRow[]): { systems: SourceSystem[]; kinds: HistoryKind[] } {
  const systems = [...new Set(rows.map((r) => r.system))].sort();
  const have = new Set(rows.map((r) => r.kind));
  return { systems, kinds: HISTORY_KINDS.filter((k) => have.has(k)) };
}
