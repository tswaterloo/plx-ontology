// What a record page says first, derived from the object page payload only: the facts by
// their field, the lead sentence, at most two badges, four numbers, the key facts, and the
// lists (messages by thread, activity by month, related by type). Display only: nothing
// is recomputed that the server did not state; an unknown value is left out, never guessed.

import { currencyCode, formatDate, formatMoney, plural, readable, recordLabel, typeLabel } from "./format";
import { etaShown as etaHeadline } from "./eta";
import { lateness, recordState, type Shown, type Status } from "./status";
import type { Fact, LinkedItem, ObjectPage, ObjectRef, PoTrail, Source, TimelineEvent } from "./types";

export interface RecordFact {
  key: string;
  label: string;
  value: string;
  source?: Source;
}

/** A fact's field key ("bc_status" -> "status"), its label and value ("Status (BC): Open"). */
export function factMap(facts: Fact[] | undefined): Map<string, RecordFact> {
  const out = new Map<string, RecordFact>();
  for (const f of facts ?? []) {
    const m = /^([^:]{1,60}):\s?([\s\S]*)$/.exec(readable(f.text));
    if (!m) continue;
    const src = f.sources[0];
    const rawKey = Object.keys(src?.fields ?? {})[0] ?? m[1];
    const key = rawKey
      .replace(/^(?:row\.)/, "")
      .replace(/([a-z])([A-Z])/g, "$1_$2")
      .toLowerCase()
      .replace(/^(?:bc|portal|sage)_/, "")
      .replace(/[^a-z0-9]+/g, "_");
    out.set(key, { key, label: m[1].replace(/\s*\((?:BC|Portal|Sage)\)\s*$/, "").trim(), value: m[2].trim(), source: src });
  }
  return out;
}

const get = (f: Map<string, RecordFact>, ...keys: string[]) => {
  for (const k of keys) {
    const v = f.get(k)?.value;
    if (v !== undefined && v !== "") return v;
  }
  return undefined;
};

/** "1,993.33 CAD" / "1993.33" + currency -> "$1,993.33 CAD"; anything else as given. */
export function moneyText(value: string | undefined, currency?: string): string | undefined {
  if (!value) return undefined;
  const m = /^(-?[\d,]+(?:\.\d+)?)\s*([A-Za-z$]{3,4})?$/.exec(value.trim());
  if (!m) return value;
  const cur = m[2] ? currencyCode(m[2]) : currency;
  return cur ? formatMoney(m[1].replace(/,/g, ""), cur) : m[1];
}

const isZero = (v: string | undefined) => !!v && /^-?0+(?:[.,]0+)?(?:\s*[A-Z]{3})?$/.test(v.trim());

/** BC's "Blocked" field: "" / "_x0020_" / "No" are not blocked; "All", "Ship", "Invoice" are. */
export function isBlocked(v: string | undefined): boolean {
  const t = (v ?? "").replace(/_x0020_/g, " ").trim();
  return !!t && !/^(?:no|none|false|0)$/i.test(t);
}

export interface StatSpec {
  label: string;
  value: string;
  caption?: string;
  status?: Status;
  /** The stat opens this source in the drawer. */
  source?: Source;
}

export interface RecordHead {
  title: string;
  counterpart?: ObjectRef | { label: string; open_url?: undefined; object_id?: undefined; type?: undefined } | null;
  subline?: string | null;
  lead?: string | null;
  leadStatus?: Status;
  badges: Shown[];
  stats: StatSpec[];
  keyFacts: { label: string; value: string; status?: Status }[];
  askLabel: string;
}

function related(obj: ObjectPage, type: RegExp): ObjectRef[] {
  return (obj.related ?? []).filter((r) => type.test(r.type));
}

/** The receipt step's "1 of 18 KG", if the trail states one. */
export function receivedOf(trail: PoTrail | undefined): { got: string; of: string; uom: string; pct: number | null } | null {
  for (const s of trail?.steps ?? []) {
    if (s.kind !== "receipt") continue;
    const m = /(\d[\d,.]*)\s+of\s+(\d[\d,.]*)\s*([A-Za-z]+)?/.exec(s.title);
    if (m) {
      const a = Number(m[1].replace(/,/g, ""));
      const b = Number(m[2].replace(/,/g, ""));
      return { got: m[1], of: m[2], uom: (m[3] ?? "").toLowerCase(), pct: b > 0 ? Math.round((a / b) * 100) : null };
    }
  }
  return null;
}

/** The needed-by date: the due-date step's, else the verdict's. */
function neededBy(trail: PoTrail | undefined): string | null {
  for (const s of trail?.steps ?? []) {
    if (s.kind === "due_date") {
      const m = /(\d{4}-\d{2}-\d{2})/.exec(s.title);
      if (m) return m[1];
    }
  }
  const m = /needed-by date (\d{4}-\d{2}-\d{2})|requested-by date (\d{4}-\d{2}-\d{2})/.exec(trail?.verdict ?? "");
  return m ? (m[1] ?? m[2]) : null;
}

/** "Mar 28, 2026" or "2026-03-28" -> "28 Mar 2026". */
function asDate(v: string | undefined): string | undefined {
  if (!v) return undefined;
  if (/^\d{4}-\d{2}-\d{2}/.test(v)) return formatDate(v);
  const d = new Date(`${v} 12:00 UTC`);
  return Number.isNaN(d.getTime()) ? v : formatDate(d.toISOString().slice(0, 10));
}

function poHead(obj: ObjectPage, f: Map<string, RecordFact>): RecordHead {
  const trail = (obj.trail?.trails ?? [])[0];
  const supplierRef = related(obj, /^(?:Supplier|Vendor)$/)[0];
  const supplier = get(f, "vendor_name", "supplier_name", "supplier") ?? supplierRef?.label;
  const status = get(f, "status");
  const draft = /^draft$/i.test(status ?? "");
  const conflict = (obj.conflicts ?? []).find((c) => /status/i.test(c.field));
  // no trail: the page's own status says the lateness ("Late by 12 days …")
  const statusDays = /(\d+)\s+days?\b/.exec(obj.status?.text ?? "")?.[1];
  const late = trail
    ? lateness(trail.state, trail.days_late, { draft })
    : /^(?:late|on_time|not_due)$/.test(obj.status?.state ?? "")
      ? lateness(obj.status.state, obj.status.state === "late" && statusDays ? Number(statusDays) : null)
      : null;
  const etaShown = trail?.eta ? etaHeadline(trail.eta) : null;
  const got = receivedOf(trail);
  const needed = neededBy(trail);
  const currency = get(f, "currency");
  let lead: string | null = null;
  let leadStatus: Status | undefined;
  if (conflict && conflict.values.length >= 2) {
    lead = `${conflict.values.map((v) => `${systemOf(v.source)} says ${v.value}`).join("; ")}. Check with Purchasing before acting.`;
    leadStatus = "suspect";
  } else if (draft) {
    lead = "Draft in Business Central; not sent to the supplier yet.";
  } else if (trail?.state === "late") {
    const who = supplier ?? "The supplier";
    const confirmed = trail.eta?.state === "confirmed";
    const parts = [
      typeof trail.days_late === "number" ? `${trail.days_late} ${trail.days_late === 1 ? "day" : "days"} late.` : "Late.",
      confirmed ? `${who} has confirmed a date` : trail.eta?.state === "lead_only" ? `${who} mentioned a date but hasn't confirmed it` : `${who} hasn't confirmed a date`,
    ];
    lead = `${parts[0]} ${parts[1]}${got ? `, and ${got.got} of ${got.of}${got.uom ? ` ${got.uom}` : ""} ${Number(got.got) === 1 ? "has" : "have"} arrived` : ""}.`;
  } else if (trail?.state === "on_time") {
    lead = `On time.${needed ? ` Due ${formatDate(needed)}.` : ""}`;
  } else if (trail?.state === "not_due") {
    lead = `Not due yet.${needed ? ` Needed by ${formatDate(needed)}.` : ""}`;
  } else if (/received|complete|closed/i.test(status ?? "")) {
    lead = `${status}.`;
  }
  const badges = [late, etaShown].filter((b): b is Shown => !!b).slice(0, 2);
  const total = moneyText(get(f, "total_cost", "total_amount_including_tax", "total_amount", "total"), currency);
  const ordered = asDate(get(f, "order_date", "document_date"));
  const stats: StatSpec[] = [];
  if (total) stats.push({ label: "Total", value: total.replace(/\s+[A-Z]{3}$/, ""), caption: currency ?? total.split(" ").pop() });
  if (needed)
    stats.push({
      label: "Needed by",
      value: formatDate(needed),
      caption: trail?.state === "late" && typeof trail.days_late === "number" ? `${trail.days_late} days ago` : undefined,
      status: trail?.state === "late" ? "late" : undefined,
    });
  if (got) stats.push({ label: "Received", value: `${got.got} of ${got.of}${got.uom ? ` ${got.uom}` : ""}`, caption: got.pct !== null ? `${got.pct}%` : undefined, status: got.pct !== null && got.pct < 100 && trail?.state === "late" ? "risk" : undefined });
  if (ordered) stats.push({ label: "Ordered", value: ordered, caption: f.get("order_date")?.source ? "Open in Business Central" : undefined, source: f.get("order_date")?.source });
  const code = get(f, "vendor_number", "supplier_number", "vendor_no");
  const keyFacts = [
    { label: "Status", value: status ?? "" },
    { label: "Supplier", value: [supplier, code ? `(${code})` : null].filter(Boolean).join(" ") },
    { label: "Currency", value: currency ?? "" },
    { label: "Buyer", value: get(f, "purchaser", "buyer", "owner") ?? "" },
    { label: "Your reference", value: get(f, "your_reference", "vendor_invoice_number") ?? "" },
    { label: "Portal number", value: get(f, "fm_po_number", "portal_po_number", "portal_number") ?? "" },
  ];
  const number = recordLabel(obj.type, obj.label);
  return {
    title: number,
    counterpart: supplierRef ?? (supplier ? { label: supplier } : null),
    subline: usefulSummary(obj, supplier),
    lead: lead ?? fallbackLead(obj),
    leadStatus,
    badges,
    stats,
    keyFacts,
    askLabel: number,
  };
}

function systemOf(s: Source): string {
  return s.system === "BC" ? "Business Central" : s.system === "Portal" ? "The Portal" : s.system;
}

/** The server's summary, kept only when it adds a fact (not the type, the name, or the status again). */
function usefulSummary(obj: ObjectPage, counterpart?: string): string | null {
  const s = readable(obj.summary).trim();
  if (!s) return null;
  const t = typeLabel(obj.type).toLowerCase();
  if (s.toLowerCase() === t || s === obj.type || s === obj.label) return null;
  if (counterpart && s.startsWith(counterpart)) return null;
  if (/no supplier confirmation|not established/i.test(s)) return null;
  return s;
}

/** The status sentence when it says something; "not established" says nothing. */
function fallbackLead(obj: ObjectPage): string | null {
  if (!obj.status || /not_established|not_applicable|^read$/i.test(obj.status.state)) return null;
  return readable(obj.status.text);
}

function supplierHead(obj: ObjectPage, f: Map<string, RecordFact>): RecordHead {
  const name = obj.label;
  const blocked = isBlocked(get(f, "blocked"));
  const balance = get(f, "balance", "balance_due");
  const currency = get(f, "currency");
  const pos = related(obj, /^PurchaseOrder$/);
  const lateRows = (obj.late_lines ?? []).filter((l) => l.days_late !== 0);
  const days = /Late by (\d+) days/i.exec(obj.status?.text ?? "")?.[1];
  const lateN = new Set(lateRows.map((l) => l.line)).size || (obj.status?.state === "late" ? 1 : 0);
  const owe = balance === undefined ? null : isZero(balance) ? `Nothing is owed to ${name}.` : `We owe ${moneyText(balance, currency)}.`;
  const lead = [
    blocked ? `${name} is blocked in Business Central.` : null,
    lateN > 0 ? `${plural(lateN, "purchase order")} ${lateN === 1 ? "is" : "are"} late${days ? `, by ${lateN === 1 ? "" : "up to "}${days} days` : ""}.` : pos.length ? `No late purchase orders. ${plural(pos.length, "order")} on record.` : null,
    owe,
  ]
    .filter(Boolean)
    .join(" ");
  const lastMsg = latestMessage(obj);
  const stats: StatSpec[] = [
    { label: "POs on record", value: String(pos.length) },
    { label: "Late", value: String(lateN), status: lateN > 0 ? "late" : undefined, caption: days ? `up to ${days} days` : undefined },
  ];
  if (balance !== undefined) stats.push({ label: "We owe", value: moneyText(balance, currency) ?? balance });
  stats.push({ label: "Last message", value: lastMsg ? formatDate(lastMsg.at, { short: true }) : "none found", caption: lastMsg?.label, source: lastMsg?.source });
  return {
    title: name,
    subline: null,
    lead: lead || null,
    badges: blocked ? [{ status: "late", label: "Blocked" }] : [],
    stats,
    keyFacts: [
      { label: "Code", value: get(f, "code", "vendor_number", "number") ?? "" },
      { label: "Currency", value: currency ?? "" },
      { label: "Blocked", value: blocked ? "Blocked in Business Central" : f.has("blocked") ? "No" : "", status: blocked ? "late" : undefined },
      { label: "Payment terms", value: get(f, "payment_terms", "payment_terms_code") ?? "" },
    ],
    askLabel: name,
  };
}

function latestMessage(obj: ObjectPage): { at: string; label: string; source: Source } | null {
  const msgs = (obj.linked ?? []).filter((l) => (l.kind === "email" || l.kind === "teams") && l.at);
  msgs.sort((a, b) => (b.at ?? "").localeCompare(a.at ?? ""));
  return msgs[0] ? { at: msgs[0].at!, label: threadSubject(msgs[0].label), source: msgs[0].source } : null;
}

function customerHead(obj: ObjectPage, f: Map<string, RecordFact>): RecordHead {
  const c = obj.customer;
  const blocked = isBlocked(get(f, "blocked"));
  const balance = get(f, "balance_due", "balance");
  const currency = get(f, "currency");
  const limit = get(f, "credit_limit");
  const open = c?.open_orders;
  const legal = (obj.conflicts ?? []).find((x) => x.field === "name")?.values.find((v) => v.trusted)?.value;
  const codes = [get(f, "code"), get(f, "erp_code")].filter((v, i, a) => v && a.indexOf(v) === i);
  const lead = [
    blocked ? "Blocked in Business Central." : null,
    balance !== undefined ? (isZero(balance) ? "No balance owing." : `Balance ${moneyText(balance, currency)} owing.`) : null,
    typeof open === "number" ? `${plural(open, "open order")}.` : null,
  ]
    .filter(Boolean)
    .join(" ");
  const stats: StatSpec[] = [];
  if (typeof open === "number") stats.push({ label: "Open orders", value: String(open) });
  if (typeof c?.orders_total === "number") stats.push({ label: "Orders", value: String(c.orders_total) });
  else if (typeof c?.orders_at_least === "number") stats.push({ label: "Orders", value: `${c.orders_at_least}+` });
  const last = [...(c?.recent_orders ?? []), ...(c?.open_list ?? [])].map((o) => o.date).filter(Boolean).sort().pop();
  if (last) stats.push({ label: "Last order", value: formatDate(last) });
  if (balance !== undefined) stats.push({ label: "Balance", value: moneyText(balance, currency) ?? balance });
  if (limit !== undefined) stats.push({ label: "Credit limit", value: isZero(limit) ? "No limit set" : (moneyText(limit, currency) ?? limit) });
  const curConflict = (obj.conflicts ?? []).some((x) => /currency/i.test(x.field));
  return {
    title: obj.label,
    subline: [legal && legal !== obj.label ? legal : null, ...codes].filter(Boolean).join(" · ") || null,
    lead: lead || null,
    badges: blocked ? [{ status: "late", label: "Blocked" }] : [],
    stats: stats.slice(0, 4),
    keyFacts: [
      { label: "Currency", value: currency ? `${currency}${curConflict ? " ⚠" : ""}` : "", status: curConflict ? "suspect" : undefined },
      { label: "Business line", value: titleCase(get(f, "business_line")) ?? "" },
      { label: "Payment terms", value: get(f, "payment_terms", "payment_terms_code") ?? "" },
      { label: "Credit limit", value: limit === undefined ? "" : isZero(limit) ? "No credit limit set" : (moneyText(limit, currency) ?? limit) },
      { label: "Blocked", value: blocked ? "Blocked in Business Central" : f.has("blocked") ? "No" : "", status: blocked ? "late" : undefined },
    ],
    askLabel: obj.label,
  };
}

function titleCase(v: string | undefined): string | undefined {
  if (!v) return v;
  return v === v.toUpperCase() ? v[0] + v.slice(1).toLowerCase() : v;
}

/** The production steps an assembly order went through (from its activity) and those not on record. */
export function productionSteps(obj: ObjectPage): { label: string; at?: string; done: boolean }[] {
  const verbs = ["Created", "Allocated", "Manufactured", "Committed", "Realized", "Filled", "Shipped"];
  const seen = new Map<string, string>();
  for (const e of obj.timeline ?? []) {
    const verb = /^(\w+):/.exec(e.title)?.[1];
    if (verb && verbs.includes(verb) && e.at && (!seen.has(verb) || seen.get(verb)! < e.at)) seen.set(verb, e.at);
  }
  const out: { label: string; at?: string; done: boolean }[] = verbs.filter((v) => seen.has(v)).map((v) => ({ label: v, at: seen.get(v), done: true }));
  for (const n of obj.not_found ?? []) {
    const step = /^(\w+) of /.exec(n.what)?.[1];
    if (step && !out.some((o) => o.label.toLowerCase().startsWith(step.toLowerCase().slice(0, 4)))) out.push({ label: step, done: false });
  }
  return out;
}

function aoHead(obj: ObjectPage, f: Map<string, RecordFact>): RecordHead {
  const status = get(f, "status");
  const product = related(obj, /^(?:Product|Item)$/)[0];
  const batch = related(obj, /^Batch$/)[0];
  const missing = (obj.not_found ?? []).map((n) => /^(\w+) of /.exec(n.what)?.[1]).filter(Boolean) as string[];
  const steps = productionSteps(obj);
  const realized = steps.find((s) => s.label === status && s.at);
  const missingWords = missing.length ? `${joinWords(missing.map((m, i) => (i === 0 ? m : m.toLowerCase())))} ${missing.length === 1 ? "isn't" : "aren't"} on record.` : "";
  const lead = [status ? `${status}${realized?.at ? ` on ${formatDate(realized.at, { short: true })}` : ""}.` : null, missingWords || null].filter(Boolean).join(" ");
  const made = get(f, "made_quantity", "produced_quantity", "quantity_produced", "actual_quantity", "output_quantity");
  const ordered = get(f, "ordered_pieces", "ordered_quantity", "quantity");
  const expected = get(f, "expected_quantity");
  const stats: StatSpec[] = [];
  if (ordered) stats.push({ label: "Ordered", value: `${Number(ordered).toLocaleString("en-CA")} pcs` });
  if (expected !== undefined) stats.push({ label: "Expected", value: expected });
  stats.push({ label: "Made", value: made ?? "not recorded", status: made ? undefined : "risk" });
  if (batch) stats.push({ label: "Batch", value: batch.label });
  return {
    title: obj.label,
    counterpart: product ?? null,
    subline: null,
    lead: lead || fallbackLead(obj),
    badges: [
      status ? { status: recordState(status), label: status } : null,
      missing.length ? { status: "risk" as Status, label: `${plural(missing.length, "step")} missing` } : null,
    ].filter((b): b is Shown => !!b),
    stats,
    keyFacts: [
      { label: "Status", value: status ?? "" },
      { label: "Product", value: product?.label ?? "" },
      { label: "Sample quantity", value: get(f, "sample_quantity") ?? "" },
    ],
    askLabel: obj.label,
  };
}

function joinWords(xs: string[]): string {
  return xs.length <= 1 ? (xs[0] ?? "") : `${xs.slice(0, -1).join(", ")} and ${xs[xs.length - 1]}`;
}

const MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"];

/** A fact's date written any way ("Oct 28, 2026", "2026-10-28") reads like every other date ("28 Oct 2026"). */
export function factValue(value: string): string {
  const v = value.trim();
  if (/^\d{4}-\d{2}-\d{2}$/.test(v)) return formatDate(v) || v;
  if (/^-?[\d,]+\.\d+\s[A-Z]{3}$/.test(v)) return moneyText(v) ?? value; // "25,113.600 USD" -> "$25,113.60 USD"
  const m = /^([A-Za-z]{3})[a-z]*\.? (\d{1,2}), (\d{4})$/.exec(v);
  const mon = m ? MONTHS.indexOf(m[1].toLowerCase()) : -1;
  if (!m || mon < 0) return value;
  return formatDate(`${m[3]}-${String(mon + 1).padStart(2, "0")}-${m[2].padStart(2, "0")}`) || value;
}

/** Row keys a person never reads: surrogate ids and sync stamps ("Ord uniq", "Row version"). */
const INTERNAL_FACT = /(?:^|_)(?:\w*uniq|guid|row_?version|etag|system_id|last_modified_date_time)$/i;

/** Every other type: its status sentence, its facts as key facts. */
function genericHead(obj: ObjectPage, f: Map<string, RecordFact>): RecordHead {
  const s = obj.status && !/not_established|^read$/.test(obj.status.state) ? { status: recordState(obj.status.state), label: shortState(obj.status.state) } : null;
  return {
    title: recordLabel(obj.type, obj.label),
    subline: usefulSummary(obj),
    lead: fallbackLead(obj),
    badges: s && s.label ? [s] : [],
    stats: [],
    keyFacts: [...f.values()]
      .filter((x) => x.label.toLowerCase() !== "name" && !INTERNAL_FACT.test(x.key) && !/\buniq\b/i.test(x.label))
      .slice(0, 6)
      .map((x) => ({ label: x.label, value: factValue(x.value) })),
    askLabel: recordLabel(obj.type, obj.label),
  };
}

function shortState(state: string): string {
  const w = state.replace(/_/g, " ");
  return w.length > 24 ? "" : w[0].toUpperCase() + w.slice(1);
}

/** The page's head, by record type. */
export function recordHead(obj: ObjectPage): RecordHead {
  const f = factMap(obj.facts);
  if (/^PurchaseOrder$/.test(obj.type)) return poHead(obj, f);
  if (/^(?:Supplier|Vendor)$/.test(obj.type)) return supplierHead(obj, f);
  if (/^Customer$/.test(obj.type)) return customerHead(obj, f);
  if (/^(?:AssemblyOrder|ManufacturingOrder)$/.test(obj.type)) return aoHead(obj, f);
  return genericHead(obj, f);
}

/** A subject without "RE:"/"FW:" prefixes. */
export function threadSubject(label: string): string {
  return label.replace(/^(?:(?:RE|FW|FWD|AW|TR)\s*:\s*)+/i, "").trim();
}

export interface Thread {
  subject: string;
  count: number;
  latest?: string | null;
  kind: LinkedItem["kind"];
  items: LinkedItem[];
}

/** Messages and documents grouped by thread subject, latest first; a generic "Email message" stays its own row. */
export function groupLinked(items: LinkedItem[]): Thread[] {
  const map = new Map<string, Thread>();
  for (const l of items) {
    const subject = threadSubject(l.label) || l.label;
    const key = `${l.kind}|${subject.toLowerCase()}`;
    const t = map.get(key) ?? { subject, count: 0, latest: null, kind: l.kind, items: [] };
    t.count += 1;
    t.items.push(l);
    if (l.at && (!t.latest || l.at > t.latest)) t.latest = l.at;
    map.set(key, t);
  }
  return [...map.values()].sort((a, b) => (b.latest ?? "").localeCompare(a.latest ?? ""));
}

export interface MonthGroup {
  month: string;
  rows: { event: TimelineEvent; count: number }[];
}

/** Activity by month, newest first, repeats of one title in a month collapsed ("Required ×40"). */
export function groupTimeline(events: TimelineEvent[]): MonthGroup[] {
  const sorted = [...events].sort((a, b) => (b.at ?? "").localeCompare(a.at ?? ""));
  const months: MonthGroup[] = [];
  for (const e of sorted) {
    const month = e.at ? new Date(`${e.at.slice(0, 7)}-15T12:00:00Z`).toLocaleDateString("en-GB", { month: "long", year: "numeric", timeZone: "UTC" }) : "Undated";
    let g = months[months.length - 1];
    if (!g || g.month !== month) {
      g = { month, rows: [] };
      months.push(g);
    }
    const verb = /^([^:]+):/.exec(e.title)?.[1] ?? e.title;
    const same = g.rows.find((r) => (/^([^:]+):/.exec(r.event.title)?.[1] ?? r.event.title) === verb && r.event.system === e.system);
    if (same) same.count += 1;
    else g.rows.push({ event: e, count: 1 });
  }
  return months;
}

/** Related records: one row per label and type, rows named only by their type dropped, grouped by type with counts. */
export function groupRelated(refs: ObjectRef[]): { type: string; label: string; items: (ObjectRef & { times: number })[]; unnamed: number }[] {
  const byType = new Map<string, { type: string; label: string; items: (ObjectRef & { times: number })[]; unnamed: number }>();
  for (const r of refs) {
    const tl = typeLabel(r.type);
    const g = byType.get(r.type) ?? { type: r.type, label: tl, items: [], unnamed: 0 };
    const generic = r.label.trim().toLowerCase() === tl.toLowerCase() || r.label.trim().toLowerCase().replace(/\s+/g, "") === r.type.toLowerCase() || /^(?:PO line|Receipt|Aged (?:payable|receivable))$/i.test(r.label.trim());
    if (generic) g.unnamed += 1;
    else {
      const same = g.items.find((x) => x.label === r.label);
      if (same) same.times += 1;
      else g.items.push({ ...r, times: 1 });
    }
    byType.set(r.type, g);
  }
  return [...byType.values()];
}
