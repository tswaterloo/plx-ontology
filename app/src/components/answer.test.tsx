import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Answer, Fact, Hidden, Source } from "@/lib/types";
import { AnswerCard, bulletFact, lateRows, type Turn } from "./answer-card";
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
  it("is named by its record, never the environment, and opens the evidence drawer on click or tap (no new tab)", async () => {
    vi.stubGlobal("fetch", vi.fn(() => new Promise(() => undefined)));
    const { container } = render(<SourceChip source={{ ...bc, label: "BC production" }} />);
    const chip = screen.getByRole("button", { name: /Business Central, BC · Receipt/ });
    expect(chip).not.toHaveTextContent("production");
    expect(container.querySelector('a[target="_blank"]')).toBeNull();
    await userEvent.click(chip);
    expect(await screen.findByTestId("evidence-drawer")).toBeInTheDocument();
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByTestId("evidence-drawer")).toBeNull();
    // focus returns to the chip
    expect(document.activeElement).toBe(chip);
  });

  it("is not a link when the source URL is unsafe, and opens nothing without a handle", () => {
    const { container } = render(<SourceChip source={{ ...bc, handle: "", open_url: "javascript:alert(1)" }} />);
    expect(screen.queryByRole("link")).toBeNull();
    expect(screen.queryByRole("button")).toBeNull();
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
    expect(within(card).getAllByText("Used")).toHaveLength(1);
    expect(card).toHaveTextContent("Systems disagree on quantity received");
    expect(card).toHaveTextContent("BC is the system of record for receipts.");
    expect(card).toHaveTextContent("Copied nightly from BC.");
  });

  it("reads a Sage order total as money and the table by its plain name", () => {
    const sage = { ...bc, system: "Portal" as const, handle: "api://portal.sage_oe_order_header/8041409", label: "Portal Oe order header" };
    render(<ConflictCard conflict={{ field: "total_amount", values: [{ value: "25113.600", source: sage, trusted: true, why: "" }, { value: "19881.6", source: { ...sage, handle: "api://portal.app_sales_order/x", label: "Portal Sales order" }, trusted: false, why: "" }] }} />);
    const card = screen.getByTestId("conflict-card");
    expect(card).toHaveTextContent("25,113.60");
    expect(card).toHaveTextContent("19,881.60");
    expect(card).not.toHaveTextContent(/Oe order header/i);
    expect(card).toHaveTextContent("Sage order");
  });

  it("never claims a trusted value when none is marked", () => {
    render(<ConflictCard conflict={{ field: "date", values: [{ value: "a", source: bc, trusted: false, why: "" }, { value: "b", source: bc, trusted: false, why: "" }] }} />);
    expect(screen.queryByText("Used")).toBeNull();
    expect(screen.getByTestId("conflict-card")).toHaveTextContent("Neither value is confirmed");
    expect(screen.getByTestId("conflict-card")).toHaveTextContent("Neither value is confirmed, so neither is used.");
  });

  it("shows dates as dates, never ISO, and leaves other text alone", () => {
    render(<ConflictCard conflict={{ field: "expected_receipt_date", values: [{ value: "2025-10-17", source: bc, trusted: true, why: "" }, { value: "pending", source: bc, trusted: false, why: "" }] }} />);
    const card = screen.getByTestId("conflict-card");
    expect(card).toHaveTextContent("17 Oct 2025");
    expect(card).not.toHaveTextContent("2025-10-17");
    expect(card).toHaveTextContent("pending");
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

  it("shows an Ask's drafts in full, an email's send only behind its preview, and nothing sent on render", async () => {
    const fetch = vi.fn(() => new Promise(() => undefined));
    vi.stubGlobal("fetch", fetch);
    const drafts: Answer["drafts"] = [
      { id: "ask:a1:0", kind: "email", title: "Late shipment", email_subject: "Late shipment", body: "Hello [supplier name],\nPlease confirm a ship date.", details: [], actions: [{ kind: "send_email", label: "Send email", outbound: true }], missing: ["Add the recipient's address before sending."], sent: false },
      { id: "ask:a1:1", kind: "payment", title: "Pay the freight invoice", email_subject: null, body: "What: Pay the freight invoice\nAmount: [amount]", details: [{ label: "What", value: "Pay the freight invoice" }, { label: "Amount", value: "[amount]" }], actions: [{ kind: "assign", label: "Assign", outbound: false }], missing: [], sent: false },
    ];
    render(<AnswerCard turn={{ id: "t", question: "q", phase: "done", streamed: "", answer: { ...answer, drafts } }} />);
    const shown = screen.getAllByTestId("ask-draft");
    expect(shown[0]).toHaveTextContent("Please confirm a ship date.");
    expect(within(shown[1]).getByTestId("ask-draft-details")).toHaveTextContent("Amount[amount]");
    // the send opens its preview first; no confirm is ever posted from rendering or opening it
    await userEvent.click(within(shown[0]).getByTestId("action-send_email"));
    const urls = fetch.mock.calls.map((c) => String((c as unknown[])[0]));
    expect(urls.some((u) => u.includes("/api/actions/preview"))).toBe(true);
    expect(urls.some((u) => u.includes("/api/actions/confirm"))).toBe(false);
  });

  it("renders markdown as text (never literal ** or list dashes), the lead first and the rest as facts", () => {
    const md = "PO 1 is a **Draft** in Business Central.\n\n**Portal copy:** says Complete.\n\n- **Pick-up email:** ready on 2 Sep\n- **Invoice:** not linked";
    render(<AnswerCard turn={{ id: "t", question: "q", phase: "done", streamed: "", answer: { ...answer, direct_answer: md } }} />);
    const card = screen.getByTestId("answer-card");
    expect(card).not.toHaveTextContent("**");
    expect(card).not.toHaveTextContent(/(^|\s)- \S/);
    const lead = screen.getByTestId("direct-answer");
    expect(lead).toHaveTextContent("PO 1 is a Draft in Business Central.");
    expect(within(lead).getByText("Draft").tagName).toBe("STRONG");
    const facts = screen.getByTestId("fact-list");
    expect(facts).toHaveTextContent("Pick-up email: ready on 2 Sep");
    expect(facts).toHaveTextContent("Invoice: not linked");
  });

  it("never leads with a field dump, labels a passage by its source and says money with its currency", () => {
    const mail: Source = { system: "Email", handle: "graph-mail://b/1", label: "Email message", fields: { "row.from": "ross@vendor.example" } };
    const a: Answer = {
      ...answer,
      direct_answer: "PO number: 106093\nStatus: Draft\nTotal cost: 9680",
      subjects: [{ object_id: "PurchaseOrder:9", type: "PurchaseOrder", label: "106093", open_url: "/object/PurchaseOrder/9" }],
      facts: [
        { text: "Currency (BC): USD", sources: [bc], check: "read_from_record" },
        { text: "Total cost: 9680", sources: [bc], check: "read_from_record" },
        { text: "Body text: next cheque run on October 25", sources: [mail], check: "read_from_record" },
      ],
    };
    render(<AnswerCard turn={{ id: "t", question: "q", phase: "done", streamed: "", answer: a }} />);
    const lead = screen.getByTestId("direct-answer");
    expect(lead).toHaveTextContent("PO 106093:");
    const card = screen.getByTestId("answer-card");
    expect(card).not.toHaveTextContent("Body text");
    expect(card).not.toHaveTextContent("(BC)");
    expect(card).toHaveTextContent("Email from ross@vendor.example: “next cheque run on October 25”");
    expect(card).toHaveTextContent("Total cost: $9,680.00 USD");
  });

  it("shows data age as one grey line: no capture states, no amber unless a used source is over a week old", () => {
    const fresh = {
      as_of: "2026-10-29T10:34:00Z",
      sources: [
        { system: "BC" as const, as_of: "2026-10-31T23:51:00Z", age_seconds: 80000, stale: false, capture: "complete" as const },
        { system: "Email" as const, as_of: "2026-10-30T15:11:00Z", age_seconds: 200000, stale: true, capture: "partial" as const },
        { system: "MC" as const, as_of: "2026-10-01T00:00:00Z", age_seconds: 2700000, stale: true, capture: "failed" as const },
      ],
    };
    render(<AnswerCard turn={{ id: "t", question: "q", phase: "done", streamed: "", answer: { ...answer, freshness: fresh } }} />);
    const f = screen.getByTestId("freshness");
    expect(f).toHaveTextContent(/^Data as of 29 Oct/);
    expect(f).not.toHaveTextContent(/Out of date|capture|continuing/i);
    expect(screen.queryByTestId("freshness-old")).toBeNull();
    // an old source shows its date under "What I checked", not as a warning
    expect(screen.getByTestId("checked-list")).toHaveTextContent("Mission Control");
  });

  it("states the data cut-off and names each source read over a day before it", () => {
    const cut = {
      as_of: "2026-11-01T22:40:07Z",
      sources: [
        { system: "BC" as const, as_of: "2026-11-01T22:40:07Z", age_seconds: 70000, stale: false },
        { system: "Portal" as const, as_of: "2026-11-01T21:42:45Z", age_seconds: 74000, stale: false },
        { system: "Email" as const, as_of: "2026-10-30T15:11:10Z", age_seconds: 270000, stale: true },
        { system: "Teams" as const, as_of: "2026-10-30T14:18:31Z", age_seconds: 317000, stale: true },
      ],
    };
    render(<AnswerCard turn={{ id: "t", question: "q", phase: "done", streamed: "", answer: { ...answer, freshness: cut } }} />);
    expect(screen.getByTestId("freshness")).toHaveTextContent(/^Data as of 1 Nov/);
    expect(screen.getByTestId("freshness-behind")).toHaveTextContent("Teams and Email to 30 Oct");
    expect(screen.getByTestId("freshness-behind")).not.toHaveTextContent("Portal");
  });

  it("warns when search cannot find records captured after its index", () => {
    const lagging = {
      as_of: "2026-11-05T06:51:00Z",
      sources: [
        { system: "Email" as const, as_of: "2026-11-05T06:51:00Z", searchable_to: "2026-10-29T15:16:00Z", stale: false },
        { system: "Teams" as const, as_of: "2026-11-04T22:00:00Z", searchable_to: "2026-10-28T15:00:00Z", stale: false },
        { system: "File" as const, as_of: "2026-11-04T10:00:00Z", searchable_to: "2026-11-04T09:00:00Z", stale: false },
      ],
    };
    render(<AnswerCard turn={{ id: "t", question: "q", phase: "done", streamed: "", answer: { ...answer, freshness: lagging } }} />);
    const lag = screen.getByTestId("freshness-search-lag");
    expect(lag).toHaveTextContent("searchable text: Teams to 28 Oct, Email to 29 Oct");
    expect(lag).not.toHaveTextContent("File");
  });

  it("never repeats the full-read label on a card: the page says it once", () => {
    const full = { ...answer, access_label: "Viewing with full-read access" };
    render(<AnswerCard turn={{ id: "t", question: "q", phase: "done", streamed: "", answer: full }} />);
    expect(screen.queryByTestId("access-label")).toBeNull();
    expect(screen.getByTestId("answer-card")).not.toHaveTextContent("full-read");
  });

  it("says the assistant is unavailable, not that nothing was found", () => {
    const down = {
      ...answer,
      outcome: "partial" as const,
      direct_answer: "The assistant is unavailable right now (its usage limit has been reached). Here is what the records show.",
      assistant_unavailable: "its usage limit has been reached",
    };
    render(<AnswerCard turn={{ id: "t", question: "q", phase: "done", streamed: "", answer: down }} />);
    expect(screen.getByTestId("outcome-badge")).toHaveTextContent("Assistant unavailable");
    expect(screen.getByTestId("outcome-badge")).not.toHaveTextContent("Not found");
    expect(screen.getByTestId("direct-answer")).not.toHaveTextContent(/couldn't find/);
  });

  it("offers business records only when a number matches several", async () => {
    const pick = vi.fn();
    render(
      <AnswerCard
        turn={{
          id: "t",
          question: "po 106093 status",
          phase: "done",
          streamed: "",
          understanding: {
            intent: "status",
            entities: [
              {
                text: "106093",
                resolved: null,
                ambiguous: true,
                candidates: [
                  { object_id: "PurchaseOrder:x", type: "PurchaseOrder", label: "106093", score: null },
                  { object_id: "BcEntityMapping:y", type: "BcEntityMapping", label: "106093", score: null },
                ],
              },
            ],
            time_frame: null,
            rewrites: [],
          },
        }}
        onPick={pick}
      />,
    );
    expect(screen.queryByTestId("did-you-mean")).toBeNull();
    expect(screen.getByTestId("answer-card")).not.toHaveTextContent(/entity mapping/i);
  });

  it("lists the plain-words steps and shows the verdict before the answer streams", () => {
    render(
      <AnswerCard
        turn={{ id: "t", question: "q", phase: "thinking", streamed: "", steps: ["Reading your question", "Finding PO 90412", "Checking supplier confirmations in mail"], verdict: "PO 90412 is 12 days late." }}
      />,
    );
    const progress = screen.getByTestId("progress");
    expect(progress).toHaveTextContent("Finding PO 90412");
    expect(progress).toHaveTextContent("Checking supplier confirmations in mail");
    expect(screen.getByTestId("verdict")).toHaveTextContent("PO 90412 is 12 days late.");
  });

  it("marks the early verdict as not confirmed when the answer errors or is stopped", () => {
    for (const error of ["The Workbench server failed on this request. Try again.", "Stopped. Ask again whenever you're ready."]) {
      const { unmount } = render(<AnswerCard turn={{ id: "t", question: "q", phase: "error", streamed: "", steps: [], verdict: "PO 90412 is 12 days late.", error }} />);
      const shown = screen.getByTestId("direct-answer");
      expect(shown).toHaveTextContent(/^Early reading, not confirmed/);
      expect(shown).toHaveTextContent("PO 90412 is 12 days late.");
      expect(shown).toHaveClass("text-muted");
      expect(screen.getByTestId("answer-card")).toHaveTextContent(error.startsWith("Stopped") ? "Stopped." : "This answer didn't finish.");
      unmount();
    }
    // while it is still being worked out, and once it lands, the line carries no such mark
    render(<AnswerCard turn={{ id: "t", question: "q", phase: "thinking", streamed: "", steps: [], verdict: "PO 90412 is 12 days late." }} />);
    expect(screen.queryByTestId("early-reading")).toBeNull();
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
    await userEvent.click(screen.getByRole("button", { name: /5280.*Customer order/ }));
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
    expect(screen.getByTestId("outcome-badge")).toHaveTextContent("Partly answered");
    // no tick on every line: only an unconfirmed fact says so, in words
    expect(screen.getAllByText(/\(not confirmed\)/)).toHaveLength(1);
    expect(screen.getByTestId("fact-list").children[1]).toHaveTextContent("Unchecked fact. (not confirmed)");
    // a partial answer opens "What's missing"
    expect(screen.getByTestId("whats-missing")).toHaveAttribute("open");
    await userEvent.click(screen.getByRole("button", { name: /Glow Botanicals.*Customer/ }));
    expect(pick).toHaveBeenCalledWith({ object_id: "Customer:a", type: "Customer", label: "Glow Botanicals" });
    // An unsafe open_url falls back to the app's own object route.
    expect(screen.getByTestId("subject-link")).toHaveAttribute("href", "/object/PurchaseOrder/PurchaseOrder%3A1");
  });
});

describe("AnswerCard: a question that needs a pick, and a stale read", () => {
  it("shows one list of choices in readable words and says to pick one, never 'partly answered'", async () => {
    const pick = vi.fn();
    render(
      <AnswerCard
        onPick={pick}
        turn={{
          id: "t",
          question: "po 100109 - did they confirm?",
          phase: "done",
          streamed: "",
          understanding: {
            intent: "status",
            time_frame: null,
            entities: [{ text: "100109", resolved: null, ambiguous: true, candidates: [
              { object_id: "PurchaseOrder:a", type: "PurchaseOrder", label: "100109", score: 0.9 },
              { object_id: "PurchaseOrder:b", type: "PurchaseOrder", label: "100109", score: 0.9 },
            ] }],
          },
          answer: {
            ...answer,
            outcome: "partial",
            facts: [],
            direct_answer: "100109 names 2 different records that share the number. Which did you mean?",
            did_you_mean: [
              { object_id: "PurchaseOrder:a", type: "PurchaseOrder", label: "100109 in Business Central (Acme Ltd, order date 2099-07-14, po number PO100109)", open_url: "/object/PurchaseOrder/a" },
              { object_id: "PurchaseOrder:b", type: "PurchaseOrder", label: "100109 in the Portal (Other Co, order date 2099-09-30, fm po number PO5174)", open_url: "/object/PurchaseOrder/b" },
            ],
          },
        }}
      />,
    );
    expect(screen.getAllByTestId("did-you-mean")).toHaveLength(1);
    expect(screen.getByTestId("outcome-badge")).toHaveTextContent("Pick one to get the answer");
    const choice = screen.getByRole("button", { name: /Acme Ltd/ });
    expect(choice).toHaveTextContent("100109 in Business Central (Acme Ltd, order date 14 Jul 2099, PO100109)");
    expect(screen.getByRole("button", { name: /Other Co/ })).toHaveTextContent("Portal PO5174");
    expect(screen.getByTestId("did-you-mean")).not.toHaveTextContent(/2099-07-14|po number/);
    await userEvent.click(choice);
    expect(pick).toHaveBeenCalledWith(expect.objectContaining({ object_id: "PurchaseOrder:a" }));
  });

  it("says a stale source as data that may be out of date, never as 'isn't on record'", () => {
    render(
      <AnswerCard
        turn={{
          id: "t",
          question: "q",
          phase: "done",
          streamed: "",
          answer: { ...answer, outcome: "partial", not_found: [{ what: "the Portal", where_it_would_live: "stale source" }] },
        }}
      />,
    );
    expect(screen.getByTestId("outcome-badge")).toHaveTextContent("Partly answered: Portal data is out of date");
    expect(screen.getByTestId("outcome-badge")).not.toHaveTextContent("isn't on record");
  });
});

describe("Feedback", () => {
  it("posts a thumbs-down with reasons and note to /api/feedback", async () => {
    const f = vi.fn(async () => new Response(JSON.stringify({ ok: true, feedback_id: "f1", stored_in: ["feedback_log"] }), { status: 200 }));
    vi.stubGlobal("fetch", f);
    render(<AnswerCard turn={{ id: "t", question: "why late", phase: "done", streamed: "", answer }} />);
    await userEvent.click(screen.getByRole("button", { name: "No, something was wrong" }));
    expect(screen.getByRole("button", { name: "Submit feedback" })).toBeDisabled();
    await userEvent.click(screen.getByRole("button", { name: "Wrong number" }));
    await userEvent.type(screen.getByLabelText("What was wrong, or what did you expect?"), "qty is 3,000");
    await userEvent.click(within(screen.getByTestId("feedback-claims")).getAllByRole("checkbox")[0]);
    await userEvent.type(screen.getByLabelText(/The right answer is/), "3,000");
    await userEvent.click(screen.getByRole("button", { name: "Submit feedback" }));
    expect(await screen.findByTestId("feedback-thanks")).toBeInTheDocument();
    const [url, init] = f.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/feedback");
    expect(JSON.parse(init.body as string)).toEqual({
      answer_id: "a1",
      rating: -1,
      note: "[Wrong number] qty is 3,000",
      claims: [0],
      expected: "3,000",
      page: "/",
      app_version: "0.1.0",
    });
  });

  it("keeps the form and shows a plain error when sending fails", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response('{"detail":"Traceback (most recent call last)"}', { status: 500 })));
    render(<AnswerCard turn={{ id: "t", question: "q", phase: "done", streamed: "", answer }} />);
    await userEvent.click(screen.getByRole("button", { name: "Yes, this answer was right" }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("The Workbench server failed on this request");
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

describe("a long list, a repeated pick and a page read in two steps", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    sessionStorage.clear();
  });

  it("says '12 of 34 shown' with a show-all link, and offers the last pick first", async () => {
    const two = [
      { object_id: "Customer:b", type: "Customer", label: "Diversified (C01100)", open_url: "/object/Customer/b" },
      { object_id: "Customer:a", type: "Customer", label: "Diversified (DIVE11)", open_url: "/object/Customer/a" },
    ];
    const facts = Array.from({ length: 12 }, (_, i) => ({ text: `PO ${i}: late.`, sources: [] }));
    const late = { ...answer, facts, list_total: 34, did_you_mean: two };
    const pick = vi.fn();
    const { unmount } = render(<AnswerCard onPick={pick} turn={{ id: "t", question: "q", phase: "done", streamed: "", answer: late }} />);
    expect(screen.getByTestId("show-all")).toHaveTextContent("12 of 34 shown. Show all");
    expect(screen.getByTestId("show-all")).toHaveAttribute("href", "/late");
    await userEvent.click(screen.getByRole("button", { name: /DIVE11/ }));
    expect(pick).toHaveBeenCalledWith({ object_id: "Customer:a", type: "Customer", label: "Diversified (DIVE11)" });
    unmount();
    render(<AnswerCard onPick={pick} turn={{ id: "t2", question: "q", phase: "done", streamed: "", answer: late }} />);
    const buttons = within(screen.getByTestId("did-you-mean")).getAllByRole("button");
    expect(buttons[0]).toHaveTextContent("DIVE11");
    expect(buttons[0]).toHaveTextContent("last picked");
  });

  it("asks for no section when the quick read comes back whole", async () => {
    const calls: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) => {
        calls.push(String(url));
        if (String(url).includes("quick=1"))
          return new Response(JSON.stringify({
            object_id: "PurchaseOrder:1", type: "PurchaseOrder", label: "PO 1", summary: "Supplier X",
            status: { state: "open", text: "Open", sources: [] }, freshness: { as_of: "2026-10-29T11:00:00Z" }, complete: true,
          }), { status: 200 });
        return new Response(JSON.stringify({ status: "read", items: [], cards: {} }), { status: 200 });
      }),
    );
    render(<ObjectView type="PurchaseOrder" id="PurchaseOrder:1" />);
    expect(await screen.findByTestId("object-title")).toHaveTextContent("PO 1");
    expect(calls.filter((u) => u.startsWith("/api/object-section/"))).toEqual([]);
    expect(screen.queryByTestId("checking")).toBeNull();
  });

  it("paints the quick read first, a skeleton per slow section, and merges each section as it lands", async () => {
    const base = {
      object_id: "PurchaseOrder:1", type: "PurchaseOrder", label: "PO 1", summary: "Supplier X",
      status: { state: "open", text: "Open", sources: [] }, freshness: { as_of: "2026-10-29T11:00:00Z" },
    };
    const held: Record<string, (r: Response) => void> = {};
    const calls: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) => {
        calls.push(String(url));
        if (String(url).includes("quick=1")) return new Response(JSON.stringify({ ...base, complete: false }), { status: 200 });
        const name = /object-section\/([a-z_]+)\//.exec(String(url))?.[1] ?? "?";
        return new Promise<Response>((r) => (held[name] = r));
      }),
    );
    render(<ObjectView type="PurchaseOrder" id="1" />);
    expect(await screen.findByTestId("object-title")).toHaveTextContent("PO 1");
    expect(screen.getByTestId("checking")).toHaveTextContent("late lines, people, landed cost and evidence");
    for (const name of ["late", "people", "landed_cost", "trail"]) expect(screen.getAllByTestId(`section-loading-${name}`).length).toBeGreaterThan(0);
    // each section is its own call, for the record the quick read resolved
    expect(calls.filter((u) => u.startsWith("/api/object-section/")).sort()).toEqual(
      ["landed_cost", "late", "people", "trail"].map((n) => `/api/object-section/${n}/PurchaseOrder/PurchaseOrder%3A1`),
    );
    const section = (name: string, extra: object) => new Response(JSON.stringify({ object_id: "PurchaseOrder:1", section: name, applies: true, late_lines: [], ...extra }), { status: 200 });
    held.people(section("people", { people: [{ name: "Dana Ortiz", role: "buyer", sources: [] }] }));
    expect(await screen.findByTestId("people")).toHaveTextContent("Dana Ortiz");
    expect(screen.queryByTestId("section-loading-people")).toBeNull();
    expect(screen.getByTestId("checking")).toHaveTextContent("late lines, landed cost and evidence");
    held.late(section("late", { status: { state: "late", text: "Late by 3 days", sources: [] } }));
    held.landed_cost(section("landed_cost", { landed_cost: null }));
    held.trail(new Response(JSON.stringify({ code: "not_found", message: "" }), { status: 404 }));
    expect(await screen.findByTestId("checks-failed")).toHaveTextContent("Couldn't load evidence");
    expect(screen.queryByTestId("checking")).toBeNull();
    expect(document.querySelector("[data-testid^='section-loading-']")).toBeNull();
  });
});

describe("AnswerCard: the analyst's shape", () => {
  const mail: Source = { system: "Email", handle: "graph-mail://m/1", label: "Email message", open_url: null };
  const shaped: Answer = {
    ...answer,
    outcome: "partial",
    direct_answer: [
      "Yes — PO 106001 is late by 129 days.",
      "- Expected receipt 2026-06-25, still not received.",
      "- The supplier promised a new date in mail.",
      "- Ordered 2,400 kg.",
      "What I couldn't confirm: the supplier's new ship date.",
    ].join("\n"),
    facts: [
      { text: "Ordered quantity: 2400 kg", sources: [bc], check: "value_matches_record" },
      { text: "Expected receipt date: 2026-06-25", sources: [{ ...bc, handle: "bc://po/106001", label: "PO 106001" }], check: "value_matches_record" },
      { text: "Payment terms: NET30", sources: [mail], check: "lead" },
    ],
    not_found: [{ what: "Supplier new ship date", where_it_would_live: "supplier email" }],
    follow_ups: ["Who should I chase about PO 106001?", "What changed on PO 106001 recently?"],
  };

  it("leads with the verdict, gives each bullet its own fact's evidence, says what's missing once and asks the follow-ups", async () => {
    const onAsk = vi.fn();
    render(<AnswerCard turn={{ id: "t", question: "is po 106001 late", phase: "done", streamed: "", answer: shaped }} onAsk={onAsk} />);
    expect(screen.getByTestId("direct-answer")).toHaveTextContent(/^Yes — PO 106001 is late by 129 days\.$/);
    const bullets = screen.getAllByTestId("answer-bullet");
    expect(bullets).toHaveLength(3);
    // a bullet restating a gated fact carries that fact's chip; one restating none carries none
    expect(within(bullets[0]).getAllByTestId("source-chip")).toHaveLength(1);
    expect(within(bullets[1]).queryAllByTestId("source-chip")).toHaveLength(0);
    expect(within(bullets[2]).getAllByTestId("source-chip")).toHaveLength(1);
    // a fact no bullet restates is still listed, with its chip and its "not confirmed"
    const list = screen.getByTestId("fact-list");
    expect(list).toHaveTextContent("Payment terms: NET30 (not confirmed)");
    expect(list.querySelectorAll("li")).toHaveLength(4);
    // the missing line is the answer's own, said once, never glued to a bullet
    const missing = screen.getByTestId("could-not-confirm");
    expect(missing).toHaveTextContent("Not found: The supplier's new ship date.");
    expect(bullets[2]).not.toHaveTextContent("confirm");
    const asks = within(screen.getByTestId("follow-ups")).getAllByRole("button");
    expect(asks.map((b) => b.textContent)).toEqual(shaped.follow_ups);
    await userEvent.click(asks[1]);
    expect(onAsk).toHaveBeenCalledWith("What changed on PO 106001 recently?");
  });

  it("has no missing line when nothing is missing, composes one from the gaps when the text has none, and no chips without follow-ups", () => {
    const full: Answer = { ...shaped, outcome: "answered", direct_answer: "Yes — PO 106001 is late.\n- Ordered 2,400 kg.", not_found: [], follow_ups: [] };
    const { unmount } = render(<AnswerCard turn={{ id: "t", question: "q", phase: "done", streamed: "", answer: full }} />);
    expect(screen.queryByTestId("could-not-confirm")).toBeNull();
    expect(screen.queryByTestId("follow-ups")).toBeNull();
    unmount();
    const gapsOnly: Answer = { ...full, outcome: "partial", direct_answer: "PO 106001 is open.\n\nWhat I couldn't confirm from a source you can open is listed below.", not_found: shaped.not_found };
    render(<AnswerCard turn={{ id: "t", question: "q", phase: "done", streamed: "", answer: gapsOnly }} />);
    expect(screen.getByTestId("could-not-confirm")).toHaveTextContent("Not found: Supplier new ship date.");
    expect(screen.getByTestId("answer-card")).not.toHaveTextContent("is listed below");
  });

  it("a bullet never borrows the evidence of a fact whose figure it does not write", () => {
    const facts = shaped.facts!;
    expect(bulletFact("Payment terms are not on the PO.", facts, new Set())).toBeNull();
    expect(bulletFact("Payment terms NET30.", facts, new Set())).toBe(2);
    // each fact lends its chips once
    expect(bulletFact("Payment terms NET30.", facts, new Set([2]))).toBeNull();
  });

  it("shows the general part in its own labelled section: no chip, no fact line, and the cited fact keeps its line", () => {
    // "Is PO 4501 confirmed by the supplier, and how should I chase it?"
    const heading = "General knowledge (not from PLX records)";
    const fact: Fact = { text: "PO 4501 has no supplier confirmation.", sources: [{ ...bc, handle: "bc://po/4501", label: "PO 4501" }], check: "passed" };
    const mixed: Answer = {
      ...answer,
      outcome: "answered",
      direct_answer: [
        "PO 4501 has no supplier confirmation yet.",
        "",
        heading,
        "- If PO 4501 is not confirmed, the supplier should be chased by phone.",
        "- Ask for a written confirmation with a ship date.",
        "- Escalate after a week.",
        "- Keep a note of each call.",
        "- Copy the buyer.",
        "- Check the PO terms.",
      ].join("\n"),
      facts: [fact, { text: "Ordered quantity: 2400 kg", sources: [bc], check: "value_matches_record" }, { text: "Expected receipt date: 2026-06-25", sources: [bc], check: "value_matches_record" }, { text: "Payment terms: NET30", sources: [mail], check: "lead" }, { text: "Buyer: Ana", sources: [bc], check: "passed" }],
      not_found: [],
      follow_ups: [],
    };
    render(<AnswerCard turn={{ id: "t", question: "Is PO 4501 confirmed by the supplier, and how should I chase it?", phase: "done", streamed: "", answer: mixed }} />);
    const general = screen.getByTestId("general-part");
    expect(within(general).getByRole("heading")).toHaveTextContent(heading);
    expect(general).toHaveTextContent("chased by phone");
    expect(within(general).queryAllByTestId("source-chip")).toHaveLength(0);
    expect(screen.queryAllByTestId("answer-bullet")).toHaveLength(0);
    const list = screen.getByTestId("fact-list");
    expect(list).not.toHaveTextContent("chased by phone");
    expect(list).toHaveTextContent("PO 4501 has no supplier confirmation.");
    // the six general lines never push the five cited facts behind "show all"
    expect(within(list).getAllByRole("listitem")).toHaveLength(5);
    expect(screen.getByTestId("direct-answer")).not.toHaveTextContent(heading);
    // words alone never lend a fact's chip
    expect(bulletFact("The supplier has no confirmation for this order yet.", [{ ...fact, text: "The supplier has not sent a confirmation yet." }], new Set())).toBeNull();
  });

  it("follow-up chips do nothing while another question is running", () => {
    render(<AnswerCard turn={{ id: "t", question: "q", phase: "done", streamed: "", answer: shaped }} />);
    for (const b of within(screen.getByTestId("follow-ups")).getAllByRole("button")) expect(b).toBeDisabled();
  });
});

describe("answer prose", () => {
  it("spaces PO numbers one way and says the late sentence once, short", async () => {
    const { prose } = await import("./answer-card");
    expect(prose("PO100095: 111 days late. An open purchase order needed by 2026-07-14 has no supplier confirmation")).toBe(
      "PO 100095: 111 days late, needed by 14 Jul 2026; no supplier confirmation",
    );
    expect(prose("See PO 4809 and SO77120.")).toBe("See PO 4809 and SO 77120.");
  });
});

describe("a late-order list answer", () => {
  it("reads as a ledger only when every fact is a late-order line", () => {
    const line = (po: string, days: number) => ({ text: `${po}: ${days} days late. An open purchase order needed by 2026-07-14 has no supplier confirmation.`, sources: [] });
    const rows = lateRows([line("PO100095", 114), line("106001", 134), line("PO 100094", 114)]);
    expect(rows?.map((r) => [r.po, r.days, r.unconfirmed])).toEqual([["PO 100095", 114, true], ["PO 106001", 134, true], ["PO 100094", 114, true]]);
    expect(rows?.[0].due).toBe("14 Jul 2026");
    expect(lateRows([line("PO1", 3), line("PO2", 4), { text: "Supplier: DSM", sources: [] }])).toBeNull();
    expect(lateRows([line("PO1", 3)])).toBeNull();
  });
});
