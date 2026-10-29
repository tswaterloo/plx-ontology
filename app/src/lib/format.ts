// Display helpers. Money stays a decimal string end to end: it is formatted,
// never parsed into a float or recomputed in the browser.

import type { SourceSystem } from "./types";

/** Only same-origin paths and https links are clickable; anything else (http:, javascript:, data:, //host) is not. */
export function safeHref(url: string | null | undefined): string | null {
  if (!url || typeof url !== "string") return null;
  const u = url.trim();
  if (u.startsWith("/") && !u.startsWith("//") && !u.startsWith("/\\")) return u;
  try {
    const parsed = new URL(u);
    return parsed.protocol === "https:" && !parsed.username && !parsed.password ? parsed.href : null;
  } catch {
    return null;
  }
}

/** Only a same-origin path is a valid post-sign-in destination. */
export function safeNext(next: string | null | undefined): string {
  if (!next || !next.startsWith("/") || next.startsWith("//") || next.startsWith("/\\")) return "/";
  if (next.startsWith("/signin")) return "/";
  return next;
}

const DECIMAL = /^-?\d+(\.\d+)?$/;

/** "12345.5" + "CAD" -> "CA$12,345.50". Non-decimal input is shown as given. */
export function formatMoney(amount: string, currency: string, locale = "en-CA"): string {
  const a = amount.trim();
  if (!DECIMAL.test(a)) return `${a} ${currency}`.trim();
  const neg = a.startsWith("-");
  const [int, frac = ""] = (neg ? a.slice(1) : a).split(".");
  const grouped = int.replace(/^0+(?=\d)/, "").replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  const cents = (frac + "00").slice(0, Math.max(2, frac.length));
  const symbol = currencySymbol(currency, locale);
  return `${neg ? "-" : ""}${symbol}${grouped}.${cents}`;
}

function currencySymbol(currency: string, locale: string): string {
  try {
    const parts = new Intl.NumberFormat(locale, { style: "currency", currency }).formatToParts(0);
    const sym = parts.find((p) => p.type === "currency")?.value;
    return sym ?? `${currency} `;
  } catch {
    return `${currency} `;
  }
}

const TZ = "America/Toronto";

export function formatDate(iso: string | null | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const dateOnly = /^\d{4}-\d{2}-\d{2}$/.test(iso);
  return d.toLocaleDateString("en-CA", {
    month: "short",
    day: "numeric",
    year: "numeric",
    timeZone: dateOnly ? "UTC" : TZ,
  });
}

export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString("en-CA", {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
    timeZone: TZ,
  });
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
  SalesOrder: "Sales order",
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
  order: "Sales order",
  sales_order: "Sales order",
  customer: "Customer",
  supplier: "Supplier",
  item: "Item",
  lot: "Lot",
};

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
