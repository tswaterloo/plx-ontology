import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
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

const trail: EvidenceTrail = { kind: "purchase_order", title: "Evidence for PO 300001", trails: [po], hidden_steps: 2, hidden_message: "2 more steps in places you can't open." };

afterEach(() => vi.unstubAllGlobals());

describe("EvidenceTrailCard", () => {
  it("puts the verdict first (rule behind a button), then the steps newest first, four then 'Show all', each opening the evidence drawer", async () => {
    vi.stubGlobal("fetch", vi.fn(() => new Promise(() => undefined)));
    render(<EvidenceTrailCard trail={trail} />);
    const card = screen.getByTestId("evidence-trail");
    const verdict = within(card).getByTestId("trail-verdict");
    expect(verdict).toHaveTextContent("Late: open on 1 Feb 2099, past the needed-by date 15 Jan 2099 (17 days).");
    // the rule is behind "Rule", never printed under every verdict
    expect(card).not.toHaveTextContent("Rule: an open purchase order");
    await userEvent.click(within(verdict).getByRole("button", { name: /Rule/ }));
    expect(verdict).toHaveTextContent("Late means still open after the needed-by date");
    let steps = within(card).getAllByTestId("trail-step");
    expect(steps.map((s) => s.dataset.kind)).toEqual(["follow_up", "revision", "confirmation", "po_issued"]);
    await userEvent.click(within(card).getByTestId("trail-show-all"));
    steps = within(card).getAllByTestId("trail-step");
    expect(steps.map((s) => s.dataset.kind)).toEqual(["follow_up", "revision", "confirmation", "po_issued", "due_date"]);
    // a mention is said as not a confirmation; a confirmed step carries no check words
    expect(steps[0]).toHaveTextContent("Mentions this record; not a confirmation");
    expect(steps[0]).not.toHaveTextContent(/machine-read|a lead/i);
    expect(steps[2]).not.toHaveTextContent(/Read from the record/);
    // a step opens the evidence in a drawer, never a new tab
    expect(card.querySelector('a[target="_blank"]')).toBeNull();
    await userEvent.click(within(steps[2]).getByRole("button"));
    expect(await screen.findByTestId("evidence-drawer")).toBeInTheDocument();
    await userEvent.keyboard("{Escape}");
    expect(within(card).getByTestId("eta-check")).toHaveTextContent("Confirmed by supplier");
    const action = within(card).getByTestId("next-action");
    expect(action).toHaveTextContent("ask the vendor (Bob <bob@vendor.example>) for a new date");
    expect(action).toHaveTextContent("Nothing is sent or changed from here.");
    expect(within(card).getByTestId("trail-hidden")).toHaveTextContent("2 more steps in places you can't open.");
  });

  it("says an undetermined verdict in plain words, keeps the late list's own check beside it, and never repeats it as a gap", () => {
    const live = "Lateness is not stated: the due dates are not proven complete or readable: the portal_api capture is stale; lead carrier: the portal_api capture is stale; a PurchaseOrder.same_record_as link below the tier floor (lead) is no.";
    const undetermined: PoTrail = {
      ...po,
      state: "undetermined",
      days_late: null,
      verdict: live,
      eta: null,
      steps: [
        po.steps![0],
        { kind: "verdict", at: "2099-02-03", title: "On your late list as of 2099-02-03: 112 days late. An open purchase order needed by 2099-01-15 has no supplier confirmation.", detail: "The late list's own check when it last ran; the verdict below is read now.", source: src("bc://p/po/1", "BC production", "BC"), check: "computed_from_cited_records" },
        { kind: "verdict", at: null, title: live, detail: null, source: null, check: "not_established" },
      ],
      not_found: [{ what: "Whether this purchase order is late", where_it_would_live: "the due dates are not proven complete or readable: the portal_api capture is stale; lead carrier: the portal_api capture is stale; a PurchaseOrder.same_record_as link below the tier floor (lead) is not counted" }],
      next_action: null,
    };
    render(<EvidenceTrailCard trail={{ ...trail, trails: [undetermined] }} />);
    const card = screen.getByTestId("evidence-trail");
    const verdict = within(card).getByTestId("trail-verdict");
    expect(verdict).toHaveTextContent("Can't say yet whether it's late: not every due date could be confirmed: the Portal data is out of date; a possible match to another copy of this order isn't confirmed.");
    // the late list's check is shown beside the live verdict (the server adds it when they differ), never dropped
    expect(within(verdict).getByTestId("trail-verdict-beside")).toHaveTextContent("On your late list as of 3 Feb 2099: 112 days late.");
    expect(card).not.toHaveTextContent(/portal_api|tier floor|lead carrier|same_record_as/);
    // the gap that only restates the verdict is not said twice
    expect(card).not.toHaveTextContent("Whether this purchase order is late");
  });

  it("never links an unsafe source URL", () => {
    const { container } = render(<EvidenceTrailCard trail={trail} />);
    expect(container.querySelector("a[href^='javascript']")).toBeNull();
    expect(container.innerHTML).not.toContain("javascript:");
  });

  it("merges a 'Goods received' step into its partial receipt and collapses repeated messages of one thread", async () => {
    const mailStep = (at: string, title: string) => ({ kind: "follow_up" as const, at, title, source: src(`graph-mail://b/${at}`, "Email message", "Email"), check: "lead" });
    const t: PoTrail = {
      ...po,
      steps: [
        { kind: "receipt", at: "2099-06-24", title: "Goods received", source: src("bc://r/1", "BC production", "BC"), check: "read_from_record" },
        { kind: "receipt", at: null, title: "Partial receipt: 1 of 18 KG", source: src("bc://r/1", "BC production", "BC"), check: "read_from_record" },
        mailStep("2099-10-08", "Email: Billing Invoice - Payment Reminder as of 08-Oct"),
        mailStep("2099-10-14", "Email: Billing Invoice - Second Payment Reminder as of 14-Oct"),
        mailStep("2099-10-15", "Email: RE: Billing Invoice - Second Payment Reminder as of 14-Oct"),
      ],
    };
    render(<EvidenceTrailCard trail={{ ...trail, trails: [t] }} />);
    const card = screen.getByTestId("evidence-trail");
    expect(card).not.toHaveTextContent("Goods received");
    expect(card).toHaveTextContent("Receipt on 24 Jun 2099: 1 of 18 KG");
    const steps = within(card).getAllByTestId("trail-step");
    expect(steps).toHaveLength(2);
    expect(steps[0]).toHaveTextContent("3 messages: Billing Invoice");
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
    expect(screen.getByTestId("eta-check")).toHaveTextContent("No supplier confirmation found");
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
    expect(screen.getByTestId("materials")).toHaveTextContent("Not checked");
    expect(screen.getByTestId("evidence-trail")).toHaveTextContent("From the recorded links");
  });

  it("says plainly when the ETA is not determined", () => {
    render(<EvidenceTrailCard trail={{ ...trail, trails: [{ ...po, eta: { state: "not_determined", text: "Not determined: this purchase order has more dates than were read." } }] }} />);
    expect(screen.getByTestId("eta-check")).toHaveTextContent("Supplier confirmation not determined");
    expect(screen.getByTestId("eta-check")).toHaveTextContent("Not determined: this purchase order has more dates than were read.");
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
    expect(screen.getByTestId("evidence-trail")).toHaveTextContent("Evidence for PO 300001");
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
    // the verdict first, with its own caveat; no lateness rule on a why trail
    const verdict = within(card).getByTestId("trail-verdict");
    expect(verdict).toHaveTextContent("The hold or release decision is not in a record you can see");
    expect(verdict).toHaveTextContent("Not established from records you can see");
    expect(within(verdict).queryByRole("button", { name: /Rule/ })).toBeNull();
    const steps = within(card).getAllByTestId("trail-step");
    expect(steps.map((s) => s.dataset.kind)).toEqual(["decision", "communication", "record_change", "document"]);
    expect(steps.map((s) => s.querySelector("[data-step-kind]")?.textContent)).toEqual(["Decision", "Message", "Record", "Document"]);
    // each step opens its evidence in the drawer (a button), never a link to a new tab
    for (const step of steps) expect(within(step).getByRole("button")).toBeInTheDocument();
    expect(card.querySelector("a[target]")).toBeNull();
    expect(steps[1]).toHaveTextContent("Names the record in its text; not a confirmation");
    expect(steps[3]).toHaveTextContent("Test came back contaminated");
    expect(steps[0]).toHaveTextContent("Not checked against its source");
    expect(within(card).getByTestId("trail-hidden")).toHaveTextContent("2 more steps in places you can't open. There may be more.");
    expect(within(card).getByTestId("trail-incomplete")).toHaveTextContent("Latest steps only");
    const action = within(card).getByTestId("next-action");
    expect(action).toHaveTextContent("Ask Quality for the hold or release decision on B-1");
    expect(action).toHaveTextContent("Nothing is sent or changed from here.");
    expect(card).not.toHaveTextContent("Dana Reyes <");
  });
});
