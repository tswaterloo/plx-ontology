// Card actions (operator_api /api/actions/*): which fields a person edits per action, the
// status chip of a card's latest action, and when the confirm button may be pressed.
// The server re-checks everything; this only keeps the dialog honest about what it does.

import type { Shown } from "./status";
import type { ActionKind, ActionPayload, ActionPreview, ActionResult } from "./types";

/** The fields a person may edit, per action. `in_reply_to` is the card's, never edited. */
export const EDITABLE: Record<ActionKind, (keyof ActionPayload)[]> = {
  send_email: ["to", "cc", "subject", "body"],
  reply_in_thread: ["to", "cc", "body"],
  forward: ["to", "cc", "body"],
  teams_message: ["chat_id", "body"],
  create_follow_up: ["due", "note"],
  mark_done: ["note"],
  snooze: ["until"],
  dismiss: ["reason"],
};

/** Only the editable fields of a payload: what the dialog sends back as edits. */
export function edits(kind: ActionKind, payload: ActionPayload): ActionPayload {
  const out: Record<string, unknown> = {};
  for (const key of EDITABLE[kind] ?? []) {
    const value = payload[key];
    if (value !== undefined && value !== null) out[key] = value;
  }
  return out as ActionPayload;
}

/** "a@x.com, b@y.com; c@z.com" as addresses (blank parts dropped). */
export function splitAddresses(text: string): string[] {
  return text
    .split(/[\s,;]+/)
    .map((a) => a.trim())
    .filter(Boolean);
}

/** Whether two payloads carry the same editable fields (the preview still shows the form). */
export function sameEdits(kind: ActionKind, a: ActionPayload, b: ActionPayload): boolean {
  return JSON.stringify(edits(kind, a)) === JSON.stringify(edits(kind, b));
}

/** One key per dialog: a second click on Send returns the first result, never a second send. */
export function newIdempotencyKey(): string {
  const c = globalThis.crypto;
  if (c?.randomUUID) return c.randomUUID();
  const bytes = new Uint8Array(16);
  c.getRandomValues(bytes);
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}

const LOCAL_CONFIRM: Partial<Record<ActionKind, string>> = {
  create_follow_up: "Save follow-up",
  mark_done: "Mark done",
  snooze: "Snooze",
  dismiss: "Dismiss",
};

/** The confirm button's words: "Send" only when the message really goes. */
export function confirmLabel(preview: ActionPreview): string {
  if (!preview.outbound) return LOCAL_CONFIRM[preview.kind] ?? "Confirm";
  return (preview.missing ?? []).length ? "Record dry run" : "Send";
}

/** Why the confirm button is off, or null when it may be pressed. */
export function confirmBlocked(preview: ActionPreview | null, opts: { edited: boolean; unknownConfirmed: boolean; busy: boolean }): string | null {
  if (!preview) return "Preparing the preview";
  if (opts.busy) return "Working";
  if (opts.edited) return "Review your changes first";
  if ((preview.problems ?? []).length) return "Fix what is listed first";
  if ((preview.unknown_domains ?? []).length && !opts.unknownConfirmed) return "Confirm the outside recipients";
  return null;
}

/** A card's latest action as a status chip; no action yet on a drafted card reads "Drafted". */
export function actionChip(state: ActionResult | null | undefined, drafted: boolean): (Shown & { title?: string }) | null {
  if (!state) return drafted ? { status: "neutral", label: "Drafted" } : null;
  const missing = (state.missing ?? []).join("; ");
  switch (state.status) {
    case "sent":
      return { status: "ok", label: "Sent", title: state.text };
    case "failed":
      return { status: "late", label: "Send failed", title: state.text };
    case "dry_run":
      return { status: "suspect", label: "Dry run: permission missing", title: missing || state.text };
    case "pending":
      return { status: "risk", label: "Outcome unknown", title: state.text };
    case "follow_up":
      return { status: "new", label: "Follow-up set", title: state.text };
    case "done":
      return { status: "ok", label: "Done", title: state.text };
    case "snoozed":
      return { status: "neutral", label: "Snoozed", title: state.text };
    case "dismissed":
      return { status: "neutral", label: "Dismissed", title: state.text };
    default:
      return { status: "neutral", label: "Unknown state" };
  }
}

/** Cards put away for now: done, dismissed or snoozed (an ended snooze is no state at all). */
export function isPutAway(state: ActionResult | null | undefined): boolean {
  return !!state && (state.status === "done" || state.status === "dismissed" || state.status === "snoozed");
}
