// "ETA confirmed?": one plain answer per purchase order (and per line when the supplier
// promised each item its own date), from the API's ETA check and the supplier's promise.
// The supplier's date never replaces our requested-by verdict; it is said beside it.

import { formatDate } from "./format";
import type { Status } from "./status";
import type { EtaCheck, LatePo, PromiseLine, VendorPromise } from "./types";

export interface EtaShown {
  status: Status;
  label: string;
}

const days = (n: number) => `${n} ${n === 1 ? "day" : "days"}`;

/** "Confirmed by supplier: arrives 4 Nov 2026", "Possible confirmation (unconfirmed link)",
 * "No supplier confirmation found", …; null when no ETA is needed. Unknown states fail
 * closed to "not determined" (never a confirmation). */
export function etaShown(eta: EtaCheck | null | undefined): EtaShown | null {
  if (!eta || eta.state === "not_applicable") return null;
  const p = eta.promise ?? null;
  if (eta.state === "withheld") return { status: "neutral", label: "Confirmation hidden from you" };
  if (p?.link === "confirmed" || (!p && eta.state === "confirmed")) {
    if (p?.per_line && (p.lines ?? []).length > 0) return { status: "ok", label: "Confirmed by supplier, per line" };
    const day = p?.promised_date;
    return { status: "ok", label: day ? `Confirmed by supplier: arrives ${formatDate(day)}` : "Confirmed by supplier" };
  }
  if (p?.link === "unconfirmed_link" || eta.state === "lead_only") return { status: "risk", label: "Possible confirmation (unconfirmed link)" };
  switch (eta.state) {
    case "promises_disagree":
      return { status: "suspect", label: "Supplier's dates disagree" };
    case "no_confirmation":
    case "no_confirmation_found":
      return { status: "risk", label: "No supplier confirmation found" };
    default:
      return { status: "neutral", label: "Supplier confirmation not determined" };
  }
}

/** "Late vs supplier's promise by 5 days" when the promise has passed and the order (or
 * line) is not received; "Received 3 days after the supplier's promise" when it came late.
 * Only a firm promise is judged; a lead never is. */
export function promiseLate(p: Pick<VendorPromise, "state" | "days_late"> & { measured_against?: VendorPromise["measured_against"] } | null | undefined, link: VendorPromise["link"] = "confirmed"): EtaShown | null {
  if (!p || link !== "confirmed" || p.state !== "late" || typeof p.days_late !== "number" || p.days_late < 1) return null;
  if (p.measured_against === "received") return { status: "late", label: `Received ${days(p.days_late)} after the supplier's promise` };
  return { status: "late", label: `Late vs supplier's promise by ${days(p.days_late)}` };
}

/** One promised line's verdict against its own date. */
export function lineShown(l: PromiseLine, link: VendorPromise["link"]): EtaShown {
  if (link !== "confirmed") return { status: "neutral", label: "Not judged (unconfirmed link)" };
  switch (l.state) {
    case "late":
      return typeof l.days_late === "number" ? { status: "late", label: `Late by ${days(l.days_late)}` } : { status: "late", label: "Late" };
    case "not_due":
      return { status: "ok", label: "On track" };
    case "on_time":
      return { status: "ok", label: "Arrived on time" };
    case "received":
      return { status: "neutral", label: "Received" };
    default:
      return { status: "neutral", label: "Not determined" };
  }
}

export const MATCHED_BY: Record<string, string> = { item_code: "matched by item code", description: "matched by description" };

/** A late-list row's confirmation, as the list's own entry says it (linked to any copy of
 * the order): none found, found but unverified (never "none"), or confirmed; otherwise the
 * order's own page decides, as the reader. */
export function listShown(po: Pick<LatePo, "confirmation">): EtaShown | null {
  if (po.confirmation === "none_found") return { status: "risk", label: "No supplier confirmation found" };
  if (po.confirmation === "found_unverified") return { status: "suspect", label: "Confirmation found, unverified" };
  if (po.confirmation === "needs_check") return { status: "suspect", label: "Possible confirmation on a reused number: needs check" };
  if (po.confirmation === "confirmed") return { status: "ok", label: "Confirmed by supplier" };
  return null;
}
