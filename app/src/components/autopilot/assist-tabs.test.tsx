import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { forgetKeptReads } from "@/lib/use-read";

vi.mock("@/components/view-as", () => ({
  useViewingAs: () => false,
}));

vi.mock("@/components/memory-people", () => ({
  MemoryPeopleSection: () => <div data-testid="memory-people-mock" />,
}));

const mocks = vi.hoisted(() => ({
  getSuggestions: vi.fn(),
  getActionStates: vi.fn(),
  getAssistWorkflows: vi.fn(),
  createAssistWorkflow: vi.fn(),
  getMemory: vi.fn(),
  saveMemory: vi.fn(),
  forgetMemory: vi.fn(),
}));

vi.mock("@/lib/api", async (orig) => ({
  ...(await orig<typeof import("@/lib/api")>()),
  getSuggestions: (...args: unknown[]) => mocks.getSuggestions(...args),
  getActionStates: (...args: unknown[]) => mocks.getActionStates(...args),
  getAssistWorkflows: (...args: unknown[]) => mocks.getAssistWorkflows(...args),
  createAssistWorkflow: (...args: unknown[]) => mocks.createAssistWorkflow(...args),
  getMemory: (...args: unknown[]) => mocks.getMemory(...args),
  saveMemory: (...args: unknown[]) => mocks.saveMemory(...args),
  forgetMemory: (...args: unknown[]) => mocks.forgetMemory(...args),
}));

import { AssistMemoryView } from "@/components/autopilot/memory-tab";
import { AssistSuggestionsView } from "@/components/autopilot/suggestions-tab";

afterEach(() => {
  cleanup();
  forgetKeptReads();
  vi.clearAllMocks();
});

describe("Assist Suggestions and Memory tabs", () => {
  it("renders proactive suggestions and turns one into a recurring workflow in one click", async () => {
    mocks.getSuggestions.mockResolvedValue({
      status: "read",
      total: 1,
      more: 0,
      items: [
        {
          id: "sug-1",
          headline: "PO 106001 is 12 days late",
          subject: { type: "PurchaseOrder", object_id: "PurchaseOrder:bc:106001", label: "PO 106001", open_url: "/po/106001" },
          action: { text: "Chase supplier for confirmed delivery date" },
          impact: { orders: [] },
          confidence: { level: "high", why: "Confirmed from BC" },
          reasoning: [],
          missing: [],
          actions: [],
        },
      ],
    });
    mocks.getActionStates.mockResolvedValue({ cards: {} });
    mocks.getAssistWorkflows.mockResolvedValue({
      status: "ok",
      workflows: [],
      templates: [],
      routines: [
        {
          routine_id: "r-1",
          kind: "schedule",
          description: "Check late supplier POs every Monday",
          trigger: {},
          mined: {},
          validated: {},
          status: "suggested",
          suggested_name: "Monday supplier ETA sweep",
          suggested_trigger_summary: "Every Monday at 8:00 AM",
        },
      ],
    });
    mocks.createAssistWorkflow.mockResolvedValue({ workflow_id: "wf-1" });

    const { container } = render(<AssistSuggestionsView />);

    await screen.findByTestId("assist-suggestions-list");
    expect(screen.getByTestId("assist-suggestions-view")).toHaveTextContent("PO 106001 is 12 days late");
    expect(screen.getByTestId("assist-mined-routines")).toHaveTextContent("Monday supplier ETA sweep");

    fireEvent.click(screen.getByTestId("suggestion-make-workflow"));
    await waitFor(() => expect(mocks.createAssistWorkflow).toHaveBeenCalledTimes(1));
    await screen.findByTestId("suggestion-workflow-created");
    expect(container.textContent).not.toContain("\u2014");
  });

  it("defaults to Ready to send drafts, replaces weak/missing labels, and allows adding a supplier email inline", async () => {
    mocks.getSuggestions.mockResolvedValue({
      status: "read",
      total: 2,
      more: 0,
      items: [
        {
          id: "sug-ready",
          headline: "PO 106101 is 4 days late",
          subject: { type: "PurchaseOrder", object_id: "PurchaseOrder:bc:106101", label: "PO 106101", open_url: "/po/106101" },
          action: { text: "Confirm delivery date with supplier" },
          impact: { orders: [] },
          confidence: { level: "high", why: "Verified in supplier thread" },
          eta: { answer: "no", in_mail: true, text: "No date confirmed in mail" },
          draft: {
            audience: "vendor",
            channel: "email",
            subject: "PO 106101 delivery date",
            body: "Hi team, can you confirm delivery?",
            to: { name: "Jordan", address: "jordan@supplier.example" },
            sent: false,
            status: "draft_not_sent",
          },
          reasoning: [],
          missing: [],
          actions: [],
        },
        {
          id: "sug-no-email",
          headline: "PO 104002 is 150 days late",
          subject: { type: "PurchaseOrder", object_id: "PurchaseOrder:bc:104002", label: "PO 104002", open_url: "/po/104002" },
          action: { text: "Check old open purchase order" },
          impact: { orders: [] },
          confidence: { level: "low", why: "ERP due date only" },
          draft: {
            audience: "vendor",
            channel: "email",
            subject: "PO 104002 status",
            body: "Checking on this order.",
            to: null,
            sent: false,
            status: "draft_not_sent",
          },
          reasoning: [],
          missing: [],
          actions: [],
        },
      ],
    });
    mocks.getActionStates.mockResolvedValue({ cards: {} });
    mocks.getAssistWorkflows.mockResolvedValue({ status: "ok", workflows: [], templates: [], routines: [] });

    const { container } = render(<AssistSuggestionsView />);
    await screen.findByTestId("assist-suggestions-list");

    // Defaults to Ready to send (1 ready draft), hiding uncontactable 150d orphan
    expect(screen.getAllByTestId("suggestion")).toHaveLength(1);
    expect(screen.getByTestId("suggestion-evidence-pill")).toHaveTextContent("Mail verified");
    expect(container.textContent).not.toMatch(/Weak evidence|No address on record/i);

    // Switch to All to inspect the uncontactable item and enter a supplier email inline
    fireEvent.click(screen.getByTestId("suggestions-filter-all"));
    expect(screen.getAllByTestId("suggestion")).toHaveLength(2);
    expect(container.textContent).toContain("From ERP schedule");
    expect(screen.getByTestId("suggestion-needs-email")).toHaveTextContent("Needs supplier email");

    fireEvent.click(screen.getByTestId("suggestion-needs-email"));
    const input = screen.getByTestId("draft-to-input") as HTMLInputElement;
    expect(input.placeholder).toBe("Add supplier email to send");
    fireEvent.change(input, { target: { value: "ops@supplier.example" } });
    expect(input.value).toBe("ops@supplier.example");
    expect(screen.getByTestId("suggestion-send-email")).toBeInTheDocument();
    expect(screen.getByTestId("copy-draft")).toBeInTheDocument();
    expect(screen.getByTestId("suggestion-dismiss")).toBeInTheDocument();
    expect(container.textContent).not.toMatch(/Weak evidence|No address on record/i);
  });

  it("renders persistent memory with add, filter, and remove actions", async () => {
    mocks.getMemory.mockResolvedValue({
      available: true,
      items: [
        {
          id: 1,
          kind: "instruction",
          text: "Always sign off with Thanks, Jordan",
          source: "you",
          at: "2026-11-25T10:00:00Z",
          records: [],
        },
        {
          id: 2,
          kind: "preference",
          text: "Use metric units for batch weights",
          source: "assist",
          at: "2026-11-24T10:00:00Z",
          records: [],
        },
      ],
    });
    mocks.saveMemory.mockResolvedValue({ id: 3, kind: "instruction", text: "Always cc buying@petrasoap.com" });
    mocks.forgetMemory.mockResolvedValue(undefined);

    const { container } = render(<AssistMemoryView />);

    await waitFor(() => expect(screen.getAllByTestId("assist-memory-row")).toHaveLength(2));
    expect(screen.getByTestId("assist-memory-view")).toHaveTextContent("Persistent memory");

    fireEvent.change(screen.getByTestId("assist-memory-input"), {
      target: { value: "Always cc buying@petrasoap.com" },
    });
    fireEvent.click(screen.getByTestId("assist-memory-save"));
    await waitFor(() =>
      expect(mocks.saveMemory).toHaveBeenCalledWith(
        { kind: "instruction", text: "Always cc buying@petrasoap.com" },
        undefined,
      ),
    );

    fireEvent.click(screen.getAllByTestId("assist-memory-remove")[0]);
    await waitFor(() => expect(mocks.forgetMemory).toHaveBeenCalledWith(1));
    expect(container.textContent).not.toContain("\u2014");
  });
});
