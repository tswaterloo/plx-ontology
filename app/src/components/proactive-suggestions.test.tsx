import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { ProactiveSuggestion, SuggestionEta } from "@/lib/types";
import { collapsedLine, draftText, etaBadge, SuggestionRow } from "./proactive-suggestions";

const src = { system: "Email" as const, handle: "graph-mail://box/m1", label: "Email", open_url: null };

const card = (over: Partial<ProactiveSuggestion> = {}): ProactiveSuggestion => ({
  id: "pe-1",
  kind: "vendor_chase",
  event_types: ["po_late"],
  rank: 1,
  headline: "PO100 is 12 days late; vendor has not confirmed",
  subject: { object_id: "PurchaseOrder:x:1", type: "PurchaseOrder", label: "PO100", open_url: "/object/PurchaseOrder/x" },
  impact: { amount: { amount: "50069.04", currency: "CAD" }, days_late: 12, orders_at_risk: 0, orders: [] },
  eta: { answer: "no", when: null, in_mail: false, source: null, text: "No: there is no vendor confirmation on record for this PO." },
  reasoning: [{ text: "Needed by 2026-07-14.", source: null }],
  action: { text: "Ask the vendor to confirm the delivery date.", rule: "no_confirmation", suggestion_only: true },
  draft: {
    status: "draft_not_sent",
    sent: false,
    channel: "email",
    audience: "vendor",
    to: { name: "Acme Oils", address: "v@acme.example", basis: "cleared sender", source: src },
    subject: "PO100: please confirm the delivery date",
    body: "Hello Acme Oils,\n\nOur purchase order PO100 was needed by 2026-07-14.",
    facts: [{ fact: "order_number", value: "PO100", source: src }],
  },
  confidence: { level: "high", why: "the dates and status are read from cited records" },
  missing: ["The order's open lines are not listed here."],
  acknowledged: false,
  new: false,
  ...over,
});

describe("proactive suggestions", () => {
  it("never shows an unknown or new ETA answer as confirmed", () => {
    const eta = (answer: string): SuggestionEta => ({ answer: answer as SuggestionEta["answer"], when: null, in_mail: false, source: null, text: "" });
    expect(etaBadge(eta("yes"))?.status).toBe("ok");
    expect(etaBadge(eta("no"))?.label).toBe("ETA not confirmed");
    expect(etaBadge(eta("something_new"))).toEqual({ status: "neutral", label: "ETA unknown" });
    expect(etaBadge(null)).toBeNull();
  });

  it("shows the headline, next step, how we know and a draft marked not sent that copies whole", () => {
    render(<ol><SuggestionRow s={card()} position={1} /></ol>);
    expect(screen.getByTestId("suggestion-headline").textContent).toContain("12 days late");
    expect(screen.getByText(/Ask the vendor to confirm/)).toBeTruthy();
    expect(screen.getByTestId("suggestion-draft").textContent).toContain("not sent");
    expect(screen.getByTestId("suggestion-missing").textContent).toContain("open lines");
    fireEvent.click(screen.getByTestId("suggestion-toggle"));
    expect(screen.queryByTestId("suggestion-draft")).toBeNull();
    expect(draftText(card().draft!)).toBe(
      "To: Acme Oils <v@acme.example>\nSubject: PO100: please confirm the delivery date\n\nHello Acme Oils,\n\nOur purchase order PO100 was needed by 2026-07-14.",
    );
  });

  it("counts the other open findings by type, largest first", () => {
    expect(collapsedLine({ lot_near_expiry: 110, lot_on_hold: 592 })).toBe(
      "Also open, no action drafted: 592 batches on quality hold, 110 lots near expiry.",
    );
    expect(collapsedLine({})).toBe("");
  });
});
