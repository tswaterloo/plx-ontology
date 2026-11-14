// Display helpers. Money stays a decimal string end to end: it is formatted,
// never parsed into a float or recomputed in the browser.

import type { SourceSystem } from "./types";

// Any control character (C0, DEL) or backslash: browsers drop tabs and newlines from a URL
// and read "\\" as "/", so "/\n/evil.example" or "/\\evil.example" would leave the site.
const URL_UNSAFE = /[\u0000-\u001f\u007f\\]/;
// A fixed origin to resolve a path against: what matters is that it stays on that origin.
const SAME = "https://workbench.invalid";

/** A same-origin path, resolved ("/a/../b" -> "/b"); null when it is anything else. */
function samePath(u: string): string | null {
  if (!u.startsWith("/") || u.startsWith("//") || URL_UNSAFE.test(u)) return null;
  try {
    const parsed = new URL(u, SAME);
    return parsed.origin === SAME ? parsed.pathname + parsed.search + parsed.hash : null;
  } catch {
    return null;
  }
}

/** Only same-origin paths and https links are clickable; anything else (http:, javascript:, data:, //host, control characters, backslashes) is not. */
export function safeHref(url: string | null | undefined): string | null {
  if (!url || typeof url !== "string") return null;
  const u = url.trim();
  if (URL_UNSAFE.test(u)) return null;
  if (u.startsWith("/")) return samePath(u);
  try {
    const parsed = new URL(u);
    return parsed.protocol === "https:" && !parsed.username && !parsed.password ? parsed.href : null;
  } catch {
    return null;
  }
}

/** Only a same-origin path is a valid post-sign-in destination (never the sign-in page itself). */
export function safeNext(next: string | null | undefined): string {
  if (!next || typeof next !== "string") return "/";
  const path = samePath(next);
  if (!path || path === "/signin" || path.startsWith("/signin/") || path.startsWith("/signin?") || path.startsWith("/signin#")) return "/";
  return path;
}

const DECIMAL = /^-?\d+(\.\d+)?$/;

/** "CAD$", "US$", "cad" -> "CAD", "USD", "CAD"; anything else as given (trimmed). */
export function currencyCode(raw: string | null | undefined): string {
  const t = (raw ?? "").trim().toUpperCase();
  const forms: Record<string, string> = { US$: "USD", $US: "USD", C$: "CAD", CA$: "CAD", CAN$: "CAD", CDN$: "CAD", $CAN: "CAD", $CDN: "CAD" };
  if (forms[t]) return forms[t];
  const m = /^[$€£]?([A-Z]{3})[$€£]?$/.exec(t);
  return m ? m[1] : t;
}

/**
 * "12345.5" + "CAD" -> "$12,345.50 CAD": the amount always says its currency, so a
 * Canadian and a US dollar never read alike. Non-decimal input is shown as given.
 */
/**
 * The figure a binary float's noise stands for ("116.25000000000001" -> "116.25"), else null.
 * Only text that is exactly the shortest form of a double (what String(number) writes), at
 * least 15 significant digits long, within 4 units in the last place of a figure with fewer
 * digits: a real figure with many places ("100000000.1234567", "1234.5678901", a 29-digit
 * decimal) is never touched. The backend's ontology.measure.float_residue is the same rule.
 */
export function floatResidue(text: string): string | null {
  if (!/^-?\d+\.\d+$/.test(text)) return null;
  const digits = text.replace(/^-/, "").replace(".", "").replace(/^0+/, "");
  if (digits.length < 15) return null;
  const x = Number(text);
  if (!Number.isFinite(x) || String(x) !== text) return null;
  const slack = 4 * ulp(x);
  const places = text.length - text.indexOf(".") - 1;
  for (let keep = 0; keep < places && keep <= 100; keep++) {
    const near = x.toFixed(keep);
    if (Math.abs(Number(near) - x) <= slack) return near.replace(/^-(0(?:\.0*)?)$/, "$1");
  }
  return null;
}

/** The distance from |x| to the next double above it (one unit in the last place). */
function ulp(x: number): number {
  const box = new Float64Array([Math.abs(x)]);
  const bits = new BigInt64Array(box.buffer);
  bits[0] += BigInt(1);
  return box[0] - Math.abs(x);
}

/**
 * A plain decimal with its whole part grouped ("1234567.125" -> "1,234,567.125"): exact, from
 * its text, never through a float; a float's noise out first (floatResidue). Anything that is
 * not a bare decimal as given.
 */
export function groupDigits(value: string): string {
  const t = value.trim();
  if (!/^-?\d+(?:\.\d+)?$/.test(t)) return value;
  const v = floatResidue(t) ?? t;
  const neg = v.startsWith("-");
  const [int, frac] = (neg ? v.slice(1) : v).split(".");
  const whole = int.replace(/^0+(?=\d)/, "").replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  return `${neg ? "-" : ""}${whole}${frac !== undefined ? `.${frac}` : ""}`;
}

/** Each figure carrying a float's noise written as the figure it is (floatResidue). */
export function withoutFloatNoise(text: string): string {
  return text.replace(/(?<![\w.])(-?\d+\.\d{7,})(?![\w.])/g, (m) => floatResidue(m) ?? m);
}

export function formatMoney(amount: string, currency: string, locale = "en-CA"): string {
  const a = withoutFloatNoise(amount.trim());
  const code = currencyCode(currency);
  if (!DECIMAL.test(a)) return `${a} ${code}`.trim();
  const neg = a.startsWith("-");
  const [int, frac = ""] = (neg ? a.slice(1) : a).split(".");
  const grouped = int.replace(/^0+(?=\d)/, "").replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  // exact: trailing zeros past the cents carry no value ("25113.600" -> "25,113.60"), real digits stay
  const f = frac.replace(/0+$/, "");
  const cents = (f + "00").slice(0, Math.max(2, f.length));
  const symbol = currencySymbol(code, locale);
  return `${neg ? "-" : ""}${symbol}${grouped}.${cents}${code ? ` ${code}` : ""}`;
}

function currencySymbol(currency: string, locale: string): string {
  try {
    const parts = new Intl.NumberFormat(locale, { style: "currency", currency, currencyDisplay: "narrowSymbol" }).formatToParts(0);
    const sym = parts.find((p) => p.type === "currency")?.value;
    // a symbol that is the code itself adds nothing: the code follows the amount
    return sym && sym !== currency ? sym : "";
  } catch {
    return "";
  }
}

const TZ = "America/Toronto";

/** Today's calendar day where the business is (YYYY-MM-DD): after 7 p.m. in Toronto it is not yet tomorrow. */
export function businessToday(now: Date = new Date()): string {
  return now.toLocaleDateString("en-CA", { timeZone: TZ });
}

/** "29 Oct 2026" (one date style in prose and tables); "29 Oct" with `short` when the year is the current one. */
export function formatDate(iso: string | null | undefined, opts: { short?: boolean; now?: Date } = {}): string {
  if (!iso) return "";
  if (isUnsetDate(iso)) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const dateOnly = /^\d{4}-\d{2}-\d{2}$/.test(iso);
  const timeZone = dateOnly ? "UTC" : TZ;
  const year = Number(d.toLocaleDateString("en-CA", { year: "numeric", timeZone }));
  const thisYear = Number((opts.now ?? new Date()).toLocaleDateString("en-CA", { year: "numeric", timeZone: TZ }));
  return d
    .toLocaleDateString("en-GB", {
      day: "numeric",
      month: "short",
      year: opts.short && year === thisYear ? undefined : "numeric",
      timeZone,
    })
    .replace("Sept", "Sep");
}

/** BC's empty date (0001-01-01) and its kin: no date at all. */
export function isUnsetDate(v: string | null | undefined): boolean {
  return !!v && /^0{3}1-01-01(?:$|[T ])/.test(v.trim());
}

/** "9:34 AM": the clock in Eastern, as the owner reads it. */
export function clockET(d: Date): string {
  return d.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit", timeZone: TZ });
}

/** "29 Oct, 6:34 AM ET" (the year when it isn't this one): the app's one date-and-time style, in Eastern. */
export function formatDateTime(iso: string | null | undefined, now: Date = new Date()): string {
  if (!iso) return "";
  // a date with no time is that calendar day, never midnight UTC shown in Toronto (the
  // day before, "8:00 PM")
  if (/^\d{4}-\d{2}-\d{2}$/.test(iso)) return formatDate(iso);
  if (isUnsetDate(iso)) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const sameYear = d.toLocaleDateString("en-CA", { year: "numeric", timeZone: TZ }) === now.toLocaleDateString("en-CA", { year: "numeric", timeZone: TZ });
  const day = d.toLocaleDateString("en-GB", { day: "numeric", month: "short", year: sameYear ? undefined : "numeric", timeZone: TZ }).replace("Sept", "Sep");
  return `${day}, ${clockET(d)} ET`;
}

/** "12:36 PM ET" today, "9 Nov, 12:36 PM ET" before: status lines say when in Eastern, the day only when it isn't today. */
export function eastern(iso?: string | null, now: Date = new Date()): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const day = (x: Date) => x.toLocaleDateString("en-CA", { timeZone: TZ });
  return day(d) === day(now) ? `${clockET(d)} ET` : formatDateTime(iso, now);
}

/** A reliability problem's headline: the server's line up to its first "; ", with its
 * snake_case names in words ("sharepoint_lists capture" reads "sharepoint lists capture"). */
export function opsHeadline(line: string): string {
  return line.split("; ")[0].replace(/_/g, " ");
}

/** "5 min ago", "3 h ago", "2 days ago" relative to `now`. */
export function ago(iso: string, now: Date = new Date()): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const s = Math.max(0, Math.round((now.getTime() - d.getTime()) / 1000));
  if (s < 60) return "just now";
  const m = Math.round(s / 60);
  if (m < 60) return `${m} min ago`;
  const h = Math.round(m / 60);
  if (h < 24) return `${h} h ago`;
  const days = Math.round(h / 24);
  return `${days} day${days === 1 ? "" : "s"} ago`;
}

export function plural(n: number, one: string, many = `${one}s`): string {
  return `${n} ${n === 1 ? one : many}`;
}

export const SYSTEM_LABEL: Record<SourceSystem, string> = {
  BC: "Business Central",
  Portal: "Customer Portal",
  Sage: "Sage",
  FileMaker: "FileMaker",
  Email: "Email",
  Teams: "Teams",
  File: "File",
  MC: "Mission Control",
};

const TYPE_LABELS: Record<string, string> = {
  PurchaseOrder: "Purchase order",
  SalesOrder: "Customer order",
  Customer: "Customer",
  Supplier: "Supplier",
  Vendor: "Supplier",
  Item: "Item",
  Lot: "Lot",
  Person: "Person",
  Employee: "Person",
  Quote: "Quote",
  Shipment: "Shipment",
  // short forms some payloads use (AtRisk.kind)
  po: "Purchase order",
  purchase_order: "Purchase order",
  order: "Customer order",
  sales_order: "Customer order",
  customer: "Customer",
  supplier: "Supplier",
  item: "Item",
  lot: "Lot",
};

/** Server wording in a reader's words: "sales order" is a customer order everywhere. */
export function plainWords(text: string): string {
  return text.replace(/\bsales (orders?)\b/gi, (m, o: string) => (m[0] === "S" ? "Customer " : "customer ") + o);
}

/** "PurchaseOrder" -> "Purchase order"; unknown CamelCase types are split into words. */
export function typeLabel(type: string | null | undefined): string {
  if (!type) return "Record";
  if (TYPE_LABELS[type]) return TYPE_LABELS[type];
  const words = type.replace(/[_-]+/g, " ").replace(/([a-z])([A-Z])/g, "$1 $2").trim().toLowerCase();
  return words ? words[0].toUpperCase() + words.slice(1) : "Record";
}

/** The app route of an object page (same shape as operator_api's open_url). */
export function objectHref(type: string, objectId: string): string {
  return `/object/${encodeURIComponent(type)}/${encodeURIComponent(objectId)}`;
}

/** The app route that opens a cited source. */
export function sourceHref(handle: string): string {
  return `/source?handle=${encodeURIComponent(handle)}`;
}

/**
 * A short server-written note (e.g. AuthConfig.message) shown only if it reads
 * as a plain sentence: no JSON, markup, paths or stack-like text.
 */
export function plainServerNote(text: string | null | undefined): string | null {
  if (!text || typeof text !== "string") return null;
  const t = text.trim();
  if (!t || t.length > 240) return null;
  if (/[{}<>[\]\\`]|Traceback|Error:|\bat \S+:\d+|\/srv\/|\.py\b/.test(t)) return null;
  return t;
}

export function money(m: { amount: string; currency: string } | null | undefined): string {
  return m ? formatMoney(m.amount, m.currency) : "";
}

// ---- plain words for record fields and server text ------------------------------------

const ACRONYM: Record<string, string> = { po: "PO", so: "SO", ao: "AO", eta: "ETA", uom: "UOM", gl: "GL", sku: "SKU", id: "ID", url: "URL", upc: "UPC", fm: "FM", bc: "BC", qc: "QC", coa: "COA", ar: "AR", ap: "AP", gst: "GST", hst: "HST", mo: "MO", oe: "OE", sds: "SDS", tds: "TDS" };
const SYSTEM_PREFIX: Record<string, string> = { bc: "BC", portal: "Portal", fm: "FileMaker", sage: "Sage" };

/** A record field's key without its container: "row.buyFromCity" -> "buyFromCity". */
export function fieldKey(key: string): string {
  return key.replace(/^(?:row|fields|record|values)\./, "");
}

/** "row.buyFromCity" -> "Buy from city"; "bc_fully_received" -> "Fully received"; "custom_fields.fmPoNumber" -> "FM PO number". */
export function fieldLabel(key: string): string {
  const last = fieldKey(key).split(".").pop() ?? key;
  const parts = last
    .replace(/([a-z0-9])([A-Z])/g, "$1_$2")
    .toLowerCase()
    .split(/[_\s-]+/)
    .filter(Boolean);
  // a system prefix ("bc_status") is dropped: the source line names the system
  if (parts.length > 1 && SYSTEM_PREFIX[parts[0]] && parts[0] !== "fm") parts.shift();
  const words = parts.map((p) => ACRONYM[p] ?? p).join(" ");
  const said = words ? words[0].toUpperCase() + words.slice(1) : "Value";
  return said.replace(/^Erp code$/, "Code").replace(/^Vendor (name|number)$/, "Supplier $1");
}

/** Keys, hashes and plumbing of a captured record: never shown as a business field. */
const INTERNAL_KEY =
  /(?:^|\.)(?:@odata\.\w+|id|uuid|guid|etag|digest|lineage|kind|source|id_type|change_key|content_sha256|body_sha256|sha256|conversation_id|internet_message_id|parent_folder_id|mailbox|mailbox_upn|authentication_results|body_preview|body_truncated|body_status|original_url|last_seen\w*|lastModifiedDateTime|last_modified|lastModified|systemModifiedAt|systemCreatedAt|recorded_at|superseded_at|native_key|binding|external_id|line_count)$|(?:Id|_id|_ids|Ids|Guid|_sha256|_hash)$|^(?:row\.)?custom_fields\.(?:fmId|fmGeneratedPoId|source)$/;

export function isInternalField(key: string): boolean {
  return INTERNAL_KEY.test(key);
}

const ISO_STAMP = /\b(\d{4}-\d{2}-\d{2})(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?\b/g;

/** Every ISO date or timestamp in a text as "Jul 14, 2026" (the day it states). */
export function plainDates(text: string): string {
  // a timestamp is the Toronto day it falls on (02:00Z on Nov 1 is Oct 31 there); a
  // date with no time is its own day
  return text.replace(ISO_STAMP, (whole, day: string) => {
    const timed = whole.length > day.length && /[zZ]|[+-]\d{2}:?\d{2}$/.test(whole);
    const shown = formatDate(timed ? whole : day);
    return shown && shown !== day && shown !== whole ? shown : whole;
  });
}

/** A capture or system name of the engine ("portal_api", "bc") as the system a reader knows. */
const CAPTURE_NAME: Record<string, string> = {
  portal: "Portal", portal_api: "Portal", "portal api": "Portal", bc: "Business Central", business_central: "Business Central",
  mail: "email", graph_mail: "email", email: "email", teams: "Teams", sharepoint: "SharePoint", mc: "Mission Control",
  sage: "Sage", filemaker: "FileMaker", fm: "FileMaker", projection: "Workbench", mirror: "Workbench",
};
const captureName = (raw: string) => CAPTURE_NAME[raw.toLowerCase()] ?? raw.replace(/_/g, " ");

/**
 * The reasoning engine's own vocabulary (capture staleness, lead carriers, tier floors,
 * link names such as `PurchaseOrder.same_record_as`, "not proven complete") said as a
 * business reader would; the meaning is kept, only the plumbing words go. Each clause once.
 */
export function plainReasons(text: string): string {
  if (!/Not settled: |capture is stale|tier floor|lead carrier|[A-Z][a-zA-Z]+\.[a-z_]+ link|not proven complete|read here may be out of date|Lateness is not stated|projector could not read/.test(text)) return text;
  const out = text
    .replace(/^Lateness is not stated:\s*/i, "Can't say yet whether it's late: ")
    .replace(/\bNot settled: /g, "Still to confirm: ")
    .replace(/\blead carrier:\s*/gi, "")
    .replace(/\bthe ([a-z_ ]+?) capture is stale\b/gi, (_m, sys: string) => `the ${captureName(sys)} data is out of date`)
    .replace(/\bthe ([a-z_ ]+?) read here may be out of date\b/gi, (_m, sys: string) =>
      /^(projection|mirror)$/i.test(sys) ? "the Workbench copy of the records is out of date" : `the ${captureName(sys)} data is out of date`)
    .replace(/\ban? [A-Z][a-zA-Z]+\.same_record_as link below the tier floor(?: \(lead\))?(?: is (?:not counted|no)\b)?/g, "a possible match to another copy of this order isn't confirmed")
    .replace(/\ban? [A-Z][a-zA-Z]+\.([a-z_]+) link below the tier floor(?: \(lead\))?(?: is (?:not counted|no)\b)?/g, (_m, l: string) => `an unconfirmed ${l.replace(/_/g, " ")} link`)
    .replace(/\b[A-Z][a-zA-Z]+\.([a-z_]+) link\b/g, (_m, l: string) => `${l.replace(/_/g, " ")} link`)
    .replace(/\s*below the tier floor(?: \(lead\))?/gi, " (unconfirmed)")
    .replace(/\bthe due dates are not proven complete or readable\b/gi, "not every due date could be confirmed")
    .replace(/\bthe projector could not read\b/gi, "couldn't read")
    .replace(/\bnot proven complete\b/gi, "not confirmed complete");
  // "x; x" -> "x": the same reason reached twice
  // a clause's own words: after its last "x: " lead-in, without the closing stop
  const key = (c: string) => c.slice(c.lastIndexOf(": ") + 1).replace(/[.\s]+$/, "").trim().toLowerCase();
  const clauses = out.split(/;\s+/);
  const kept = clauses.filter((c, i) => clauses.findIndex((o) => key(o) === key(c)) === i);
  const joined = kept.join("; ");
  return /[.]$/.test(out) && !/[.]$/.test(joined) ? `${joined}.` : joined;
}

/** "PurchaseOrderLine:bc.purchase_order_lines:{...}" (an object key) -> "Purchase order line". */
const OBJECT_KEY = /\b([A-Z][A-Za-z]+):[a-z][\w]*\.[\w.]+:\{[^}]*\}/g;

/**
 * A server sentence as a business reader reads it: escapes decoded (_x0020_), object keys
 * as their kind, codes (LINE_NOT_INVOICED) as words, ISO dates as dates. Display only.
 */
export function readable(text: string | null | undefined): string {
  if (!text) return "";
  return plainDates(
    plainReasons(withoutFloatNoise(text))
      .replace(/_x([0-9A-Fa-f]{4})_/g, (_m, hex: string) => String.fromCharCode(parseInt(hex, 16)))
      .replace(OBJECT_KEY, (_m, type: string) => typeLabel(type))
      .replace(/\b[A-Z]{2,}(?:_[A-Z]{2,})+:\s+(?=\w)/g, "")
      .replace(/\b[A-Z]{2,}(?:_[A-Z]{2,})+\b/g, (code) => code.replace(/_/g, " ").toLowerCase()),
  );
}

/** A record field's value as shown: lists joined, escapes decoded, dates as dates. */
export function fieldValue(key: string, value: string): string {
  let v = withoutFloatNoise(value).replace(/_x([0-9A-Fa-f]{4})_/g, (_m, hex: string) => String.fromCharCode(parseInt(hex, 16))).trim();
  if (/^\[.*\]$/s.test(v)) {
    try {
      const list = JSON.parse(v);
      if (Array.isArray(list)) v = list.filter((x) => typeof x === "string" || typeof x === "number").join(", ");
    } catch {
      /* not a list: shown as written */
    }
  }
  if (/(?:^|\.)(?:currency|currencyCode|currency_code)$/i.test(key)) return currencyCode(v);
  if (isUnsetDate(v)) return "Not set";
  if (/^\d{4}-\d{2}-\d{2}(?:[T ][\d:.]+(?:Z|[+-]\d{2}:?\d{2})?)?$/.test(v)) return v.length > 10 ? formatDateTime(v) : formatDate(v);
  if (v === "true") return "yes";
  if (v === "false") return "no";
  return v;
}

/** Projection types that are mappings or plumbing, never a business record to pick. */
export function internalType(type: string | null | undefined): boolean {
  return !!type && /(?:Mapping|EntityMap|Map|Binding|Mirror|Lineage)$/.test(type);
}

// ---- what a cited source is, in a reader's words ---------------------------------------

/** BC API collections and Portal tables -> the kind of record they hold (whole names only). */
const RECORD_KIND: [RegExp, string][] = [
  [/^purchase_?order_?lines?$/i, "PO line"],
  [/^purchase_?orders?$/i, "Purchase order"],
  [/^(?:purchase_?)?receipts?$/i, "Receipt"],
  [/^purchase_?invoices?$/i, "Purchase invoice"],
  [/^sales_?orders?$/i, "Customer order"],
  [/^sales_?invoices?$/i, "Customer invoice"],
  [/^(?:vendors?|suppliers?|ap_vendor)$/i, "Supplier"],
  [/^ar_customer$/i, "AR record"],
  [/^oe_?order_?headers?$/i, "Sage order"],
  [/^oe_?order_?details?$/i, "Sage order line"],
  [/^customers?$/i, "Customer"],
  [/^(?:items?|products?)$/i, "Item"],
  [/^manufacturing_?orders?$/i, "Manufacturing order"],
  [/^assembly_?orders?$/i, "Assembly order"],
  [/^batch_?calibrations?$/i, "Batch calibration"],
  [/^(?:batch|batches|batch_?records?)$/i, "Batch"],
  [/^lots?$/i, "Lot"],
  [/^item_?ledger_?entr(?:y|ies)$/i, "Item ledger entry"],
];

function kindOf(name: string): string | null {
  const n = name.replace(/([a-z])([A-Z])/g, "$1_$2").replace(/\s+/g, "_");
  for (const [re, kind] of RECORD_KIND) if (re.test(n)) return kind;
  return null;
}

/** The kind of record a source handle points at ("bc://…/purchaseOrders/…" -> "Purchase order"), or null. */
export function recordKind(handle: string | null | undefined, label?: string | null): string | null {
  const h = handle ?? "";
  let names: string[] = [];
  if (h.startsWith("bc://")) names = h.slice(5).split(/[/@?]/);
  else if (h.startsWith("api://")) {
    const table = (h.slice(6).split("/")[0] ?? "").split(".").pop() ?? "";
    names = [table, table.replace(/^(?:app|fm|rel|gen|sage)_/, "")];
  }
  for (const n of names) {
    const k = kindOf(n);
    if (k) return k;
  }
  const l = (label ?? "").replace(/^(?:Portal|BC|Sage|FileMaker)\s+/i, "").trim();
  if (!l || /production|sandbox|^email message$|^record$/i.test(l)) return null;
  return kindOf(l) ?? l[0].toUpperCase() + l.slice(1);
}

/** "PurchaseOrder" + "106093" -> "PO 106093"; a label that already says what it is stays. */
export function recordLabel(type: string | null | undefined, label: string): string {
  // "PO PO100095" (a type word before a number that already carries it) is said once
  const l = label.trim().replace(/^(PO|SO) (?=\1[- ]?\d)/i, "").replace(/^(PO|SO)(?=\d)/, "$1 "); // "PO100095" reads as "PO 100095", like the rest
  if (/^\d[\w-]*$/.test(l)) {
    if (/^PurchaseOrder$|^purchase_order$/.test(type ?? "")) return `PO ${l}`;
    if (/^SalesOrder$|^sales_order$/.test(type ?? "")) return `SO ${l}`;
    if (/^Lot$|^lot$/.test(type ?? "")) return `Lot ${l}`;
  }
  return l.replace(/^(PO|SO) (?=\1[- ]?\d)/i, "").replace(/^AO (AO-)/, "$1").replace(/^MO (AO-)/, "MO $1");
}

/** "9680 USD" / "14,611.33 CAD" in prose -> "$9,680.00 USD": an amount that names its currency reads as money. */
export function proseMoney(text: string): string {
  return text.replace(/(^|[\s(])(-?\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|-?\d{3,}(?:\.\d{1,2})?)\s(USD|CAD|EUR|GBP)\b/g, (_m, pre: string, n: string, cur: string) => `${pre}${formatMoney(n.replace(/,/g, ""), cur)}`);
}
