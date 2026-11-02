// The one map from API states to a status, a label of at most three words, and a tone.
// Every badge, dot and rule colour in the app comes from here, so the same late PO is
// the same red on Home, in an answer and on its own page.

export type Status = "late" | "risk" | "ok" | "suspect" | "new" | "neutral";

export interface Shown {
  status: Status;
  label: string;
}

/** Tailwind classes per status: text, soft fill, line (border / left rule), dot. */
export const STATUS_CLASS: Record<Status, { fg: string; soft: string; line: string; dot: string; rule: string }> = {
  late: { fg: "text-late", soft: "bg-late-soft", line: "border-late-line", dot: "bg-late", rule: "before:bg-late-line" },
  risk: { fg: "text-risk", soft: "bg-risk-soft", line: "border-risk-line", dot: "bg-risk", rule: "before:bg-risk-line" },
  ok: { fg: "text-ok", soft: "bg-ok-soft", line: "border-ok-line", dot: "bg-ok", rule: "before:bg-ok-line" },
  suspect: { fg: "text-suspect", soft: "bg-suspect-soft", line: "border-suspect-line", dot: "bg-suspect", rule: "before:bg-suspect-line" },
  new: { fg: "text-new", soft: "bg-new-soft", line: "border-new-line", dot: "bg-new", rule: "before:bg-new-line" },
  neutral: { fg: "text-neutral", soft: "bg-neutral-soft", line: "border-neutral-line", dot: "bg-neutral", rule: "before:bg-neutral-line" },
};

/**
 * A lateness state. `draft`: the record is a draft, so "not applicable" reads "Draft".
 * Unknown states fail closed to neutral with no claim ("Lateness unknown").
 */
export function lateness(state: string | null | undefined, daysLate?: number | null, opts: { draft?: boolean } = {}): Shown | null {
  if (!state) return null;
  switch (state) {
    case "late":
      return { status: "late", label: typeof daysLate === "number" ? `${daysLate} ${daysLate === 1 ? "day" : "days"} late` : "Late" };
    case "not_due":
      return { status: "neutral", label: "Not due yet" };
    case "on_time":
      return { status: "ok", label: "On time" };
    case "not_applicable":
      return { status: "neutral", label: opts.draft ? "Draft" : "Not open" };
    case "undetermined":
    default:
      return { status: "neutral", label: "Lateness unknown" };
  }
}

/** A vendor-date (ETA) check. `not_applicable` shows no badge at all. */
export function eta(state: string | null | undefined): Shown | null {
  switch (state) {
    case "confirmed":
      return { status: "ok", label: "Date confirmed" };
    case "promises_disagree":
      return { status: "suspect", label: "Dates disagree" };
    case "lead_only":
      return { status: "risk", label: "Date not confirmed" };
    case "no_confirmation":
    case "no_confirmation_found":
      return { status: "risk", label: "No confirmed date" };
    case "withheld":
      return { status: "neutral", label: "Hidden from you" };
    case "not_determined":
      return { status: "neutral", label: "Date unknown" };
    case "not_applicable":
    case null:
    case undefined:
    case "":
      return null;
    default:
      return { status: "neutral", label: "Date unknown" };
  }
}

const BRIEFING: Record<string, Shown> = {
  po_late: { status: "late", label: "Late PO" },
  po_due: { status: "risk", label: "Due soon" },
  eta_slipped: { status: "risk", label: "Date slipped" },
  vendor_no_reply: { status: "risk", label: "No reply" },
  vendor_terms_unapplied: { status: "risk", label: "Terms not applied" },
  negative_margin: { status: "late", label: "Losing money" },
  lot_near_expiry: { status: "risk", label: "Lot expiring" },
  lot_on_hold: { status: "risk", label: "Lot on hold" },
  quote_accepted_no_order: { status: "risk", label: "Quote not ordered" },
  quantity_mismatch: { status: "suspect", label: "Quantities differ" },
  customer_order_at_risk: { status: "risk", label: "Order at risk" },
};

/** A briefing item's kind. A suspect total outranks the kind: the data is the problem. */
export function briefingKind(eventType: string, opts: { amountSuspect?: boolean; fallbackLabel?: string } = {}): Shown {
  if (opts.amountSuspect) return { status: "suspect", label: "Check the total" };
  if (BRIEFING[eventType]) return BRIEFING[eventType];
  if (eventType.startsWith("negative_margin")) return { status: "late", label: "Losing money" };
  return { status: "neutral", label: shortLabel(opts.fallbackLabel) ?? "Needs a look" };
}

/** An answer's outcome. "answered" has no badge (the confidence line says it). */
export function outcome(o: string | null | undefined): Shown | null {
  switch (o) {
    case "partial":
      return { status: "risk", label: "Partly answered" };
    case "not_found":
    case "abstained":
      return { status: "neutral", label: "Not on record" };
    case "refused":
      return { status: "neutral", label: "Can't answer" };
    default:
      return null;
  }
}

/** A free-text record state ("Open", "Released", "on_hold") as a status; unknown is neutral. */
export function recordState(state: string | null | undefined): Status {
  const s = (state ?? "").toLowerCase();
  if (!s) return "neutral";
  if (/blocked|failed|rejected|overdue|\blate\b/.test(s)) return "late";
  if (/hold|risk|unconfirmed|pending|waiting/.test(s)) return "risk";
  if (/suspect|conflict|disagree/.test(s)) return "suspect";
  if (/received|complete|closed|shipped|done|on_time|confirmed|realized|invoiced|paid/.test(s)) return "ok";
  return "neutral";
}

/** At most three words: a longer server label is cut to its first three words. */
export function shortLabel(text: string | null | undefined): string | null {
  if (!text) return null;
  const words = text.trim().split(/\s+/).filter(Boolean);
  if (words.length === 0) return null;
  return words.slice(0, 3).join(" ").replace(/[,:;.]+$/, "");
}
