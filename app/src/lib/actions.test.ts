import { describe, expect, it } from "vitest";
import { actionChip, confirmBlocked, confirmLabel, edits, isPutAway, outcomeUnknown, splitAddresses } from "./actions";
import type { ActionPreview, ActionResult } from "./types";

const preview = (over: Partial<ActionPreview> = {}): ActionPreview => ({
  card_id: "pe-1",
  kind: "send_email",
  label: "Send email",
  outbound: true,
  sender: "vince@plx.example",
  payload: { to: ["v@acme.example"], cc: [], subject: "PO100", body: "Hello", in_reply_to: null },
  payload_hash: "a".repeat(64),
  citations: [],
  uncited: [],
  unknown_domains: [],
  problems: [],
  missing: [],
  request: null,
  teams_chats: [],
  ...over,
});

const state = (status: ActionResult["status"], missing: string[] = []): ActionResult => ({
  card_id: "pe-1",
  kind: "send_email",
  status,
  text: "",
  at: "2026-11-04T15:00:00+00:00",
  seq: 1,
  missing,
  replayed: false,
});

describe("card actions", () => {
  it("sends back only the fields a person may edit (never the cited thread)", () => {
    expect(edits("reply_in_thread", { to: ["a@b.example"], body: "x", in_reply_to: "graph-mail://m/1@c", subject: "s" })).toEqual({ to: ["a@b.example"], body: "x" });
    expect(edits("dismiss", { reason: "dup", body: "x" })).toEqual({ reason: "dup" });
    expect(splitAddresses(" a@b.example; c@d.example ,, ")).toEqual(["a@b.example", "c@d.example"]);
  });

  it("says Send only when the message really goes, and holds the button until every check passes", () => {
    expect(confirmLabel(preview())).toBe("Send");
    expect(confirmLabel(preview({ missing: ["Mail.Send not granted"] }))).toBe("Record dry run");
    expect(confirmLabel(preview({ outbound: false, kind: "snooze" }))).toBe("Snooze");
    const ok = { edited: false, unknownConfirmed: false, busy: false };
    expect(confirmBlocked(preview(), ok)).toBeNull();
    expect(confirmBlocked(null, ok)).not.toBeNull();
    expect(confirmBlocked(preview(), { ...ok, busy: true })).not.toBeNull();
    expect(confirmBlocked(preview(), { ...ok, edited: true })).toBe("Review your changes first");
    expect(confirmBlocked(preview({ problems: ["Add at least one recipient."] }), ok)).not.toBeNull();
    expect(confirmBlocked(preview({ unknown_domains: ["x.example"] }), ok)).toBe("Confirm the outside recipients");
    expect(confirmBlocked(preview({ unknown_domains: ["x.example"] }), { ...ok, unknownConfirmed: true })).toBeNull();
    expect(confirmBlocked(preview(), { ...ok, mustCheckSent: true })).toBe("Check Sent Items first");
    expect(confirmBlocked(preview(), { ...ok, mustCheckSent: true, sentChecked: true })).toBeNull();
  });

  it("shows each state as a chip and unknown states fail closed", () => {
    expect(actionChip(null, true)).toEqual({ status: "neutral", label: "Drafted" });
    expect(actionChip(state("sent"), true)?.label).toBe("Sent");
    expect(actionChip(state("failed"), true)?.status).toBe("late");
    expect(actionChip(state("dry_run", ["Mail.Send not granted"]), true)).toMatchObject({ label: "Dry run: permission missing", title: "Mail.Send not granted" });
    expect(actionChip(state("something_new" as ActionResult["status"]), true)).toEqual({ status: "neutral", label: "Unknown state" });
    expect(actionChip(state("unknown"), true)?.label).toBe("Outcome unknown");
    expect(outcomeUnknown(state("unknown"))).toBe(true);
    expect(outcomeUnknown(state("failed"))).toBe(false);
    // a send still going out offers no "I checked Sent Items" resend
    expect(outcomeUnknown(state("pending"))).toBe(false);
    expect(actionChip(state("pending"), true)?.label).toBe("Sending");
    expect(isPutAway(state("snoozed"))).toBe(true);
    expect(isPutAway(state("dry_run"))).toBe(false);
  });
});
