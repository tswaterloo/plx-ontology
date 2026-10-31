import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { Answer, EvidenceTrail, PoTrail, Source } from "@/lib/types";
import { AnswerCard, type Turn } from "./answer-card";
import { EvidenceTrailCard } from "./evidence-trail";

const src = (handle: string, label: string, system: Source["system"] = "Portal"): Source => ({
  system,
  handle,
  label,
  open_url: `/source?handle=${encodeURIComponent(handle)}`,
});

const po: PoTrail = {
  subject: { object_id: "PurchaseOrder:p:1", type: "PurchaseOrder", label: "PO 300001", open_url: "/object/PurchaseOrder/1" },
  state: "late",
  days_late: 17,
  verdict: "Late: open on 2099-02-01, past the requested-by date 2099-01-15 (17 days).",
  eta: { state: "confirmed", text: "Yes: the vendor confirmed 2099-01-28 (revised 1 time).", source: src("graph-mail://b/c", "Email message", "Email") },
  steps: [
    { kind: "due_date", at: null, title: "Requested by 2099-01-15", source: src("api://fm/1", "FileMaker purchase order", "FileMaker"), check: "read_from_record" },
    { kind: "po_issued", at: "2099-01-02", title: "PO issued", source: src("api://fm/1", "FileMaker purchase order", "FileMaker"), check: "read_from_record" },
    { kind: "confirmation", at: "2099-01-05", title: "Vendor confirmed delivery by 2099-01-20", source: src("graph-mail://b/a", "Email message", "Email"), check: "read_from_record" },
    { kind: "revision", at: "2099-01-12", title: "Promise revised from 2099-01-20 to 2099-01-28", source: src("graph-mail://b/c", "Email message", "Email"), check: "read_from_record" },
    { kind: "follow_up", at: "2099-01-25T09:00:00", title: "Email from the vendor side: RE: Chasing", source: { ...src("graph-mail://b/d", "Email message", "Email"), open_url: "javascript:alert(1)" }, check: "lead", sender: "Bob <bob@vendor.example>" },
    { kind: "verdict", at: "2099-02-01", title: "Late: open on 2099-02-01, past the requested-by date 2099-01-15 (17 days).", detail: "Rule: an open purchase order is late once today is past its requested-by date.", source: src("api://fm/1", "FileMaker purchase order", "FileMaker"), check: "computed_from_cited_records" },
  ],
  hidden_steps: 2,
  may_hide_more: false,
  next_action: {
    text: "The vendor's confirmed date (2099-01-28) has passed with nothing received: ask the vendor (Bob <bob@vendor.example>) for a new date.",
    rule: "promise_passed",
    contact: "Bob <bob@vendor.example>",
    contact_source: src("graph-mail://b/d", "Email message", "Email"),
    suggestion_only: true,
  },
  not_found: [],
};

const trail: EvidenceTrail = { kind: "purchase_order", title: "How we know: PO 300001", trails: [po], hidden_steps: 2, hidden_message: "2 more steps in places you can't open." };

describe("EvidenceTrailCard", () => {
  it("lists the steps in order, each opening its source, with the ETA badge and the next step", () => {
    render(<EvidenceTrailCard trail={trail} />);
    const card = screen.getByTestId("evidence-trail");
    const steps = within(card).getAllByTestId("trail-step");
    expect(steps.map((s) => s.dataset.kind)).toEqual(["due_date", "po_issued", "confirmation", "revision", "follow_up", "verdict"]);
    const confirm = within(steps[2]).getByRole("link");
    expect(confirm).toHaveAttribute("href", "/source?handle=graph-mail%3A%2F%2Fb%2Fa");
    expect(confirm).toHaveAttribute("rel", "noopener noreferrer");
    expect(steps[5]).toHaveTextContent("Rule: an open purchase order is late");
    expect(steps[5]).toHaveTextContent("Computed from the cited records");
    expect(steps[4]).toHaveTextContent("Machine-read reference (a lead)");
    expect(within(card).getByTestId("eta-check")).toHaveTextContent("ETA confirmed by the vendor");
    expect(within(card).getByRole("link", { name: /Open the confirmation/ })).toHaveAttribute("href", "/source?handle=graph-mail%3A%2F%2Fb%2Fc");
    const action = within(card).getByTestId("next-action");
    expect(action).toHaveTextContent("ask the vendor (Bob <bob@vendor.example>) for a new date");
    expect(action).toHaveTextContent("A suggestion only: nothing has been sent or changed.");
    expect(within(card).getByTestId("trail-hidden")).toHaveTextContent("2 more steps in places you can't open.");
  });

  it("never links an unsafe source URL", () => {
    const { container } = render(<EvidenceTrailCard trail={trail} />);
    const step = screen.getAllByTestId("trail-step")[4];
    expect(within(step).queryByRole("link")).toBeNull();
    expect(container.innerHTML).not.toContain("javascript:");
  });

  it("says no confirmation plainly and shows a materials table with typed gaps", () => {
    const materials: EvidenceTrail = {
      kind: "materials",
      title: "Materials: 1 late purchase order of 2 checked",
      hidden_steps: 0,
      materials: [
        {
          assembly: { object_id: "AssemblyOrder:a:1", type: "AssemblyOrder", label: "AO 11231", open_url: "/object/AssemblyOrder/1" },
          item: { object_id: "Product:p:1", type: "Product", label: "Bottle 50ml", open_url: "/object/Product/1" },
          po: { object_id: "PurchaseOrder:p:1", type: "PurchaseOrder", label: "PO 300001", open_url: "/object/PurchaseOrder/1" },
          state: "late",
          days_late: 17,
        },
      ],
      gaps: [{ reason: "line_item_not_recorded", state: "unknown", what: "A purchase order line for Cap 20mm on AO 11231", where_it_would_live: "the item reference on the purchase order line" }],
      trails: [{ ...po, eta: { state: "no_confirmation_found", text: "No vendor confirmation found in what you can see." }, next_action: null, hidden_steps: 0 }],
    };
    render(<EvidenceTrailCard trail={materials} />);
    const table = screen.getByTestId("materials");
    expect(within(table).getByRole("link", { name: "PO 300001" })).toHaveAttribute("href", "/object/PurchaseOrder/1");
    expect(table).toHaveTextContent("17 d late");
    expect(screen.getByTestId("trail-gaps")).toHaveTextContent("A purchase order line for Cap 20mm on AO 11231: not found on record");
    expect(screen.getByTestId("eta-check")).toHaveTextContent("No vendor confirmation found");
    expect(screen.queryByTestId("next-action")).toBeNull();
    expect(screen.queryByTestId("trail-hidden")).toBeNull();
  });

  it("keeps the hidden caveats: a single PO trail's 'may be more' and a materials walk's own", () => {
    render(<EvidenceTrailCard trail={{ ...trail, trails: [{ ...po, hidden_steps: 2, may_hide_more: true }] }} />);
    const shown = screen.getAllByTestId("trail-hidden").map((n) => n.textContent);
    expect(shown).toEqual(["2 more steps in places you can't open. There may be more."]);
  });

  it("shows a materials walk's hidden caveat beside its one late-PO trail, with no claim the answer missed anything", () => {
    const one: EvidenceTrail = {
      kind: "materials",
      title: "Materials: 1 late purchase order of 3 checked",
      hidden_steps: 0,
      hidden_message: "Some steps may be in places you can't open.",
      materials: [
        {
          assembly: { object_id: "a", type: "AssemblyOrder", label: "AO-1", open_url: "/o/a" },
          item: { object_id: "i", type: "Product", label: "P-1", open_url: "/o/i" },
          po: { object_id: "p", type: "PurchaseOrder", label: "PO 5", open_url: "/o/p" },
          state: "late",
          days_late: 3,
        },
      ],
      trails: [{ ...po, hidden_steps: 0, may_hide_more: false }],
    };
    render(<EvidenceTrailCard trail={one} />);
    expect(screen.getAllByTestId("trail-hidden").map((n) => n.textContent)).toEqual(["Some steps may be in places you can't open."]);
    expect(screen.queryByTestId("trail-note")).toBeNull();
  });

  it("labels a row whose lateness was not read as such", () => {
    const r = { assembly: { object_id: "a", type: "AssemblyOrder", label: "AO-1", open_url: "/o/a" }, item: { object_id: "i", type: "Product", label: "P-1", open_url: "/o/i" }, po: { object_id: "p", type: "PurchaseOrder", label: "PO 5", open_url: "/o/p" } };
    render(<EvidenceTrailCard trail={{ kind: "materials", title: "From the recorded links: 1 purchase order for this order's materials, 0 late", hidden_steps: 0, materials: [r] }} />);
    expect(screen.getByTestId("materials")).toHaveTextContent("lateness not read");
    expect(screen.getByTestId("evidence-trail")).toHaveTextContent("From the recorded links");
  });

  it("says plainly when the ETA is not determined", () => {
    render(<EvidenceTrailCard trail={{ ...trail, trails: [{ ...po, eta: { state: "not_determined", text: "Not determined: this purchase order has more dates than were read." } }] }} />);
    expect(screen.getByTestId("eta-check")).toHaveTextContent("ETA not determined");
  });

  it("renders nothing without a trail, and an answer without one still renders", () => {
    const { container } = render(<EvidenceTrailCard trail={undefined} />);
    expect(container).toBeEmptyDOMElement();
    const answer = {
      answer_id: "a1", question: "q", conversation_id: "c", outcome: "answered", latency_ms: 1, access_label: null,
      direct_answer: "PO 300001 is 17 days late.", facts: [], not_found: [],
      hidden: { count: 0, by_owner: [] }, conflicts: [], freshness: {},
    } as Answer;
    const turn: Turn = { id: "t", question: "q", phase: "done", streamed: "", answer };
    render(<AnswerCard turn={turn} />);
    expect(screen.getByTestId("direct-answer")).toHaveTextContent("17 days late");
    expect(screen.queryByTestId("evidence-trail")).toBeNull();
    render(<AnswerCard turn={{ ...turn, id: "t2", answer: { ...answer, trail } }} />);
    expect(screen.getByTestId("evidence-trail")).toHaveTextContent("How we know: PO 300001");
  });

  it("renders a why trail: its four step kinds in order, each opening its source, the verdict, the hidden count and the suggestion", () => {
    const why: EvidenceTrail = {
      kind: "why",
      title: "Why a batch or lot is on hold, not released or expired",
      plan: "hold_release",
      steps: [
        { kind: "record_change", at: "2099-01-02", title: "Batch tested", source: src("api://b/1", "Portal batch"), check: "read_from_record" },
        { kind: "communication", at: "2099-01-03T09:00:00Z", title: "Email from Dana Reyes", source: src("graph-mail://b/m", "Email message", "Email"), check: "text_match" },
        { kind: "document", at: null, title: "Checked against the document", detail: "Test came back contaminated", source: src("original://d/1", "Document", "File"), check: "passed" },
        { kind: "decision", at: "2099-01-04", title: "Quality status changed", source: src("api://b/2", "Portal batch"), check: "unchecked" },
        { kind: "verdict", at: null, title: "The hold or release decision is not in a record you can see", source: null, check: "not_established" },
      ],
      hidden_steps: 2,
      may_hide_more: true,
      complete: false,
      next_action: { text: "Ask Quality for the hold or release decision on B-1, pointing to the test steps above.", rule: "hold_release.verdict_missing", suggestion_only: true },
    };
    render(<EvidenceTrailCard trail={why} />);
    const card = screen.getByTestId("evidence-trail");
    const steps = within(card).getAllByTestId("trail-step");
    expect(steps.map((s) => s.dataset.kind)).toEqual(["record_change", "communication", "document", "decision", "verdict"]);
    expect(steps.map((s) => within(s).getAllByText(/./)[0].textContent)).toEqual(["Record", "Message", "Document", "Decision", "Verdict"]);
    for (const step of steps.slice(0, 4)) expect(within(step).getByRole("link")).toHaveAttribute("rel", "noopener noreferrer");
    expect(within(steps[0]).getByRole("link")).toHaveAttribute("href", "/source?handle=api%3A%2F%2Fb%2F1");
    expect(within(steps[4]).queryByRole("link")).toBeNull();
    expect(steps[1]).toHaveTextContent("Names the record by text (a match, not a link)");
    expect(steps[2]).toHaveTextContent("Test came back contaminated");
    expect(steps[3]).toHaveTextContent("Not checked against its source");
    expect(steps[3]).not.toHaveTextContent("Checked against its source.");
    expect(steps[4]).toHaveTextContent("Not established from records you can see");
    expect(within(card).getByTestId("trail-hidden")).toHaveTextContent("2 more steps in places you can't open. There may be more.");
    expect(within(card).getByTestId("trail-incomplete")).toHaveTextContent("Only the latest steps are shown");
    const action = within(card).getByTestId("next-action");
    expect(action).toHaveTextContent("Ask Quality for the hold or release decision on B-1");
    expect(action).toHaveTextContent("A suggestion only: nothing has been sent or changed.");
    expect(card).not.toHaveTextContent("Dana Reyes <");
  });
});
