import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Answer, Hidden, Source } from "@/lib/types";
import { AnswerCard, type Turn } from "./answer-card";
import { ConflictCard, HiddenNotice } from "./answer-parts";
import { ObjectView } from "./object-view";
import { SourceChip } from "./source-chip";

afterEach(() => vi.unstubAllGlobals());

const bc: Source = {
  system: "BC",
  handle: "bc://purchaseReceipts/PR-1",
  label: "Receipt PR-1",
  fields: { "Quantity received": "3,000" },
  open_url: "/o/po/1",
};

describe("HiddenNotice", () => {
  it("shows the count and who to ask", () => {
    render(<HiddenNotice hidden={{ count: 4, by_owner: [{ owner_or_team: "Finance", count: 3, named: true }, { owner_or_team: "Purchasing", count: 1, named: true }] }} />);
    const n = screen.getByTestId("hidden-notice");
    expect(n).toHaveTextContent("4 more items match, but you don't have access.");
    expect(n).toHaveTextContent("Ask Finance or Purchasing");
  });

  it("renders nothing about hidden items beyond counts and owners, even if the payload carries more", () => {
    const leaky = {
      count: 1,
      by_owner: [{ owner_or_team: "Finance", count: 1, title: "SECRET-TITLE", snippet: "SECRET-SNIPPET" }],
      items: [{ title: "SECRET-TITLE", sender: "SECRET-SENDER", snippet: "SECRET-SNIPPET" }],
      title: "SECRET-TITLE",
      message: "SECRET-TITLE from SECRET-SENDER",
    } as unknown as Hidden;
    const { container } = render(<HiddenNotice hidden={leaky} />);
    expect(container.textContent).toContain("1 more item matches");
    expect(container.innerHTML).not.toMatch(/SECRET/);
  });

  it("renders nothing for zero, missing or malformed counts", () => {
    for (const h of [{ count: 0, by_owner: [] }, undefined, null, { count: NaN, by_owner: [] }, { count: -3, by_owner: [] }]) {
      const { container } = render(<HiddenNotice hidden={h as Hidden} />);
      expect(container).toBeEmptyDOMElement();
    }
  });
});

describe("SourceChip", () => {
  it("links to a safe source in a new tab", () => {
    render(<SourceChip source={bc} />);
    const a = screen.getByRole("link", { name: /Business Central, Receipt PR-1/ });
    expect(a).toHaveAttribute("href", "/o/po/1");
    expect(a).toHaveAttribute("rel", "noopener noreferrer");
  });

  it("is not a link when the source URL is unsafe", () => {
    const { container } = render(<SourceChip source={{ ...bc, open_url: "javascript:alert(1)" }} />);
    expect(screen.queryByRole("link")).toBeNull();
    expect(container.innerHTML).not.toContain("javascript:");
  });

  it("shows the record fields on keyboard focus", async () => {
    render(<SourceChip source={bc} />);
    await userEvent.tab();
    expect(await screen.findByTestId("source-preview")).toHaveTextContent("Quantity received3,000");
  });
});

describe("ConflictCard", () => {
  it("shows both values, marks the trusted one and says why", () => {
    render(
      <ConflictCard
        conflict={{
          field: "quantity received",
          values: [
            { value: "3,000", source: bc, trusted: true, why: "BC is the system of record for receipts." },
            { value: "3,200", source: { ...bc, system: "Portal", label: "Order 1" }, trusted: false, why: "Copied nightly from BC." },
          ],
        }}
      />,
    );
    const card = screen.getByTestId("conflict-card");
    expect(card).toHaveTextContent("3,000");
    expect(card).toHaveTextContent("3,200");
    expect(within(card).getAllByText("Trusted")).toHaveLength(1);
    expect(card).toHaveTextContent("BC is the system of record for receipts.");
    expect(card).toHaveTextContent("Copied nightly from BC.");
  });

  it("never claims a trusted value when none is marked", () => {
    render(<ConflictCard conflict={{ field: "date", values: [{ value: "a", source: bc, trusted: false, why: "" }, { value: "b", source: bc, trusted: false, why: "" }] }} />);
    expect(screen.queryByText("Trusted")).toBeNull();
    expect(screen.getByTestId("conflict-card")).toHaveTextContent("Neither value is confirmed");
  });
});

const answer: Answer = {
  answer_id: "a1",
  question: "why late",
  conversation_id: "c1",
  outcome: "answered",
  latency_ms: 900,
  access_label: null,
  direct_answer: "PO 1 is 3 days late.",
  facts: [{ text: "Due Oct 26.", sources: [bc], check: "value_matches_record" }],
  not_found: [{ what: "A new ship date", where_it_would_live: "supplier email" }],
  hidden: { count: 2, by_owner: [{ owner_or_team: "Finance", count: 2, named: true }] },
  conflicts: [],
  freshness: { as_of: "2026-10-29T11:00:00Z" },
};

describe("AnswerCard", () => {
  it("puts the direct answer first, then facts, not-found, hidden, freshness and feedback", () => {
    const turn: Turn = { id: "t", question: "q", phase: "done", streamed: "PO 1 is", answer };
    render(<AnswerCard turn={turn} />);
    const card = screen.getByTestId("answer-card");
    const order = ["direct-answer", "source-chip", "not-found", "hidden-notice", "freshness", "feedback"].map((id) =>
      card.innerHTML.indexOf(`data-testid="${id}"`),
    );
    expect(order.every((i) => i >= 0)).toBe(true);
    expect([...order].sort((a, b) => a - b)).toEqual(order);
    expect(card).toHaveAttribute("aria-busy", "false");
  });

  it("labels an answer served under a full-read grant, and only that one", () => {
    const plain: Turn = { id: "t", question: "q", phase: "done", streamed: "", answer };
    const { unmount } = render(<AnswerCard turn={plain} />);
    expect(screen.queryByTestId("access-label")).toBeNull();
    unmount();
    const full = { ...answer, access_label: "Viewing with full-read access" };
    render(<AnswerCard turn={{ ...plain, answer: full }} />);
    expect(screen.getByTestId("access-label")).toHaveTextContent("Viewing with full-read access");
  });

  it("shows streamed text with a busy state before the answer lands", () => {
    render(<AnswerCard turn={{ id: "t", question: "q", phase: "streaming", streamed: "PO 1 is", status: "Checking mail" }} />);
    expect(screen.getByTestId("direct-answer")).toHaveTextContent("PO 1 is");
    expect(screen.getByTestId("answer-card")).toHaveAttribute("aria-busy", "true");
    expect(screen.queryByTestId("feedback")).toBeNull();
  });

  it("offers each candidate of an ambiguous entity", async () => {
    const pick = vi.fn();
    render(
      <AnswerCard
        onPick={pick}
        turn={{
          id: "t",
          question: "5280",
          phase: "done",
          streamed: "",
          understanding: {
            intent: "status",
            time_frame: null,
            entities: [
              {
                text: "5280",
                resolved: null,
                ambiguous: true,
                candidates: [
                  { object_id: "PurchaseOrder:5280", type: "PurchaseOrder", label: "5280", score: 0.9 },
                  { object_id: "SalesOrder:5280", type: "SalesOrder", label: "5280", score: 0.9 },
                ],
              },
            ],
          },
        }}
      />,
    );
    await userEvent.click(screen.getByRole("button", { name: /5280.*Sales order/ }));
    expect(pick).toHaveBeenCalledWith(expect.objectContaining({ object_id: "SalesOrder:5280" }));
  });
});

describe("AnswerCard payload parts", () => {
  it("marks only checked facts as checked, offers did-you-mean picks and links the subjects", async () => {
    const pick = vi.fn();
    render(
      <AnswerCard
        onPick={pick}
        turn={{
          id: "t",
          question: "q",
          phase: "done",
          streamed: "",
          answer: {
            ...answer,
            outcome: "partial",
            facts: [
              { text: "Checked fact.", sources: [bc], check: "value_matches_record" },
              { text: "Unchecked fact.", sources: [bc] },
            ],
            did_you_mean: [{ object_id: "Customer:a", type: "Customer", label: "Glow Botanicals", open_url: "/object/Customer/Customer%3Aa" }],
            subjects: [{ object_id: "PurchaseOrder:1", type: "PurchaseOrder", label: "PO 1", open_url: "javascript:alert(1)" }],
          },
        }}
      />,
    );
    expect(screen.getByText("Partial answer")).toBeInTheDocument();
    expect(screen.getAllByLabelText("Checked against its source")).toHaveLength(1);
    await userEvent.click(screen.getByRole("button", { name: /Glow Botanicals.*Customer/ }));
    expect(pick).toHaveBeenCalledWith({ object_id: "Customer:a", type: "Customer", label: "Glow Botanicals" });
    // An unsafe open_url falls back to the app's own object route.
    expect(screen.getByTestId("subject-link")).toHaveAttribute("href", "/object/PurchaseOrder/PurchaseOrder%3A1");
  });
});

describe("Feedback", () => {
  it("posts a thumbs-down with reasons and note to /api/feedback", async () => {
    const f = vi.fn(async () => new Response(JSON.stringify({ ok: true, feedback_id: "f1", stored_in: ["feedback_log"] }), { status: 200 }));
    vi.stubGlobal("fetch", f);
    render(<AnswerCard turn={{ id: "t", question: "why late", phase: "done", streamed: "", answer }} />);
    await userEvent.click(screen.getByRole("button", { name: "No, something was wrong" }));
    expect(screen.getByRole("button", { name: "Send feedback" })).toBeDisabled();
    await userEvent.click(screen.getByRole("button", { name: "Wrong number" }));
    await userEvent.type(screen.getByLabelText("Tell us more"), "qty is 3,000");
    await userEvent.click(screen.getByRole("button", { name: "Send feedback" }));
    expect(await screen.findByTestId("feedback-thanks")).toBeInTheDocument();
    const [url, init] = f.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/feedback");
    expect(JSON.parse(init.body as string)).toEqual({
      answer_id: "a1",
      rating: -1,
      note: "[Wrong number] qty is 3,000",
    });
  });

  it("keeps the form and shows a plain error when sending fails", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response('{"detail":"Traceback (most recent call last)"}', { status: 500 })));
    render(<AnswerCard turn={{ id: "t", question: "q", phase: "done", streamed: "", answer }} />);
    await userEvent.click(screen.getByRole("button", { name: "Yes, this answer was right" }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Something went wrong on our side");
    expect(alert).not.toHaveTextContent("Traceback");
  });
});

describe("optional and unknown hidden counts", () => {
  it("says some items may be hidden when no count may be given, and never shows a number", () => {
    render(<HiddenNotice hidden={{ count: null, by_owner: [{ owner_or_team: "Finance", count: 2, named: true }] }} />);
    const n = screen.getByTestId("hidden-notice");
    expect(n).toHaveTextContent("Some items may be hidden from you");
    expect(n).toHaveTextContent("Ask Finance if you need them.");
    expect(n.textContent).not.toMatch(/\b0\b|null|undefined/);
  });

  it("counts the unnamed private bucket but names nobody to ask for it", () => {
    render(<HiddenNotice hidden={{ count: 3, by_owner: [{ owner_or_team: "Other people's mail, chats and files", count: 3, named: false }] }} />);
    const n = screen.getByTestId("hidden-notice");
    expect(n).toHaveTextContent("3 more items match");
    expect(n).toHaveTextContent("They are in other people's private mail, chats or files.");
    expect(n).not.toHaveTextContent("Ask");
  });

  it("lists named owners to ask and the private items separately", () => {
    render(
      <HiddenNotice
        hidden={{
          count: 4,
          by_owner: [
            { owner_or_team: "Purchasing", count: 3, named: true },
            { owner_or_team: "private", count: 1, named: false },
          ],
        }}
      />,
    );
    const n = screen.getByTestId("hidden-notice");
    expect(n).toHaveTextContent("Ask Purchasing if you need them.");
    expect(n).toHaveTextContent("1 item is in other people's private mail, chats or files.");
  });

  it("shows nothing when the payload carries no hidden field", () => {
    const { container } = render(<HiddenNotice hidden={undefined} />);
    expect(container).toBeEmptyDOMElement();
  });
});

describe("server states on the ask stream", () => {
  it("shows busy as a calm banner with retry, and ask-switched-off without retry", () => {
    const retry = vi.fn();
    const { rerender } = render(
      <AnswerCard onRetry={retry} turn={{ id: "t", question: "q", phase: "error", streamed: "", error: "The Workbench is answering several questions right now. Try again in a minute.", notice: "busy" }} />,
    );
    expect(screen.getByTestId("server-notice")).toHaveTextContent("answering several questions");
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
    rerender(<AnswerCard onRetry={retry} turn={{ id: "t", question: "q", phase: "error", streamed: "", error: "Asking questions isn't switched on", notice: "off" }} />);
    expect(screen.getByTestId("server-notice")).toHaveTextContent("isn't switched on");
    expect(screen.queryByRole("button", { name: "Try again" })).toBeNull();
  });
});

describe("object page with optional sections absent", () => {
  it("omits the people and hidden sections instead of showing empty boxes or 0", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        new Response(
          JSON.stringify({
            object_id: "PurchaseOrder:1",
            type: "PurchaseOrder",
            label: "PO 1",
            summary: "Supplier X",
            status: { state: "open", text: "Open", sources: [] },
            freshness: { as_of: "2026-10-29T11:00:00Z" },
          }),
          { status: 200 },
        ),
      ),
    );
    render(<ObjectView type="PurchaseOrder" id="PurchaseOrder:1" />);
    expect(await screen.findByTestId("object-title")).toHaveTextContent("PO 1");
    expect(screen.queryByTestId("people")).toBeNull();
    expect(screen.queryByTestId("hidden-notice")).toBeNull();
    expect(screen.queryByTestId("late-lines")).toBeNull();
    expect(document.body.textContent).not.toMatch(/\b0 (more )?items?\b/);
  });
});
