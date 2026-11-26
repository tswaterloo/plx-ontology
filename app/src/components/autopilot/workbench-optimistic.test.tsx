import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { DraftList, PreparedItem } from "@/lib/api";
import { forgetKeptReads } from "@/lib/use-read";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn() }),
}));

vi.mock("@/components/feed-note", () => ({
  FeedNote: () => null,
}));

const mocks = vi.hoisted(() => ({
  getAutopilotDrafts: vi.fn(),
  getAssistJobs: vi.fn(),
  decideAutopilotItem: vi.fn(),
  saveMemory: vi.fn(),
}));

vi.mock("@/lib/api", async (orig) => ({
  ...(await orig<typeof import("@/lib/api")>()),
  getAutopilotDrafts: (...args: unknown[]) => mocks.getAutopilotDrafts(...args),
  getAssistJobs: (...args: unknown[]) => mocks.getAssistJobs(...args),
  decideAutopilotItem: (...args: unknown[]) => mocks.decideAutopilotItem(...args),
  saveMemory: (...args: unknown[]) => mocks.saveMemory(...args),
}));

import { AssistWorkbench } from "@/components/autopilot/workbench";

function todoItem(id: string, subject: string, over: Partial<PreparedItem> = {}): PreparedItem {
  return {
    item_id: id,
    sender: "dana@example.com",
    channel: "mail",
    subject,
    message: "Please follow up.",
    kind: "todo",
    to: [],
    cc: [],
    body: "",
    edited: false,
    ask: "",
    facts: [],
    summary: subject,
    created_at: "2026-11-25T10:00:00Z",
    todos: [{ system: "bc", record: "PO 100", what: "Check date" }],
    ...over,
  };
}

afterEach(() => {
  cleanup();
  forgetKeptReads();
  vi.clearAllMocks();
});

describe("AssistWorkbench optimistic item actions", () => {
  it("immediately removes the item and shows toast on Make it a task without re-fetching drafts", async () => {
    const items = [todoItem("item_1", "First item"), todoItem("item_2", "Second item")];
    const drafts: DraftList = { items };
    mocks.getAutopilotDrafts.mockResolvedValue(drafts);
    mocks.getAssistJobs.mockResolvedValue({ jobs: [], status: "ok" });

    let resolveMemory!: () => void;
    let resolveDecide!: () => void;
    mocks.saveMemory.mockImplementation(() => new Promise<void>((r) => (resolveMemory = r)));
    mocks.decideAutopilotItem.mockImplementation(() => new Promise<void>((r) => (resolveDecide = r)));

    render(<AssistWorkbench linked={null} />);
    await waitFor(() => expect(screen.getAllByTestId("assist-queue-item")).toHaveLength(2));
    expect(mocks.getAutopilotDrafts).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole("button", { name: "More" }));
    fireEvent.click(screen.getByRole("menuitem", { name: "Make it a task" }));

    // Instant optimistic removal and toast BEFORE network calls resolve
    expect(screen.getAllByTestId("assist-queue-item")).toHaveLength(1);
    expect(screen.getByTestId("assist-toast")).toHaveTextContent("Added to your tasks");

    await act(async () => {
      resolveMemory();
      await Promise.resolve();
      resolveDecide();
      await Promise.resolve();
    });

    // Still no extra blocking reload of /api/autopilot/drafts
    expect(mocks.getAutopilotDrafts).toHaveBeenCalledTimes(1);
    expect(mocks.saveMemory).toHaveBeenCalledTimes(1);
    expect(mocks.decideAutopilotItem).toHaveBeenCalledTimes(1);
  });

  it("rolls back the item and shows an error banner when Done fails", async () => {
    const items = [todoItem("item_1", "First item"), todoItem("item_2", "Second item")];
    mocks.getAutopilotDrafts.mockResolvedValue({ items });
    mocks.getAssistJobs.mockResolvedValue({ jobs: [], status: "ok" });

    let rejectDecide!: (err: Error) => void;
    mocks.decideAutopilotItem.mockImplementation(() => new Promise((_r, rej) => (rejectDecide = rej)));

    render(<AssistWorkbench linked={null} />);
    await waitFor(() => expect(screen.getAllByTestId("assist-queue-item")).toHaveLength(2));

    fireEvent.click(screen.getByRole("button", { name: "Done" }));

    // Optimistically removed at 0 ms
    expect(screen.getAllByTestId("assist-queue-item")).toHaveLength(1);
    expect(screen.getByTestId("assist-toast")).toHaveTextContent("Marked done");

    // Simulate backend failure -> item rolls back cleanly with error banner
    await act(async () => {
      rejectDecide(new Error("Server unavailable"));
      await Promise.resolve();
    });

    await waitFor(() => expect(screen.getAllByTestId("assist-queue-item")).toHaveLength(2));
    expect(screen.queryByTestId("assist-toast")).toBeNull();
    expect(screen.getByTestId("assist-action-error")).toBeInTheDocument();
  });

  it("defaults to Mine filter hiding other people's items, collapses RE: thread duplicates, and switches to Team", async () => {
    const mine = todoItem("item_mine", "Workbench instant Teams updates");
    const dup = todoItem("item_dup", "RE: Workbench instant Teams updates");
    const other: PreparedItem = {
      ...todoItem("item_other", "Other person's task"),
      note: "Draft on request.\nThis is on Ross's list, not yours. Only you can see this.",
    };
    mocks.getAutopilotDrafts.mockResolvedValue({ items: [mine, dup, other], older: 42 });
    mocks.getAssistJobs.mockResolvedValue({ jobs: [], status: "ok" });

    render(<AssistWorkbench linked={null} />);
    await waitFor(() => expect(screen.getAllByTestId("assist-queue-item")).toHaveLength(1));
    expect(screen.getByTestId("assist-queue-item")).toHaveTextContent("Workbench instant Teams updates");
    expect(screen.getByTestId("assist-queue-item")).toHaveTextContent("2 messages");
    expect(screen.getByTestId("assist-older")).toHaveTextContent("Show older items");

    // More menu opens with Make it a task and Not needed visible
    fireEvent.click(screen.getByRole("button", { name: "More" }));
    expect(screen.getByRole("menuitem", { name: "Make it a task" })).toBeInTheDocument();
    expect(screen.getByRole("menuitem", { name: "Not needed" })).toBeInTheDocument();

    // Switching to Team shows the other person's item too
    fireEvent.click(screen.getByRole("button", { name: "Team" }));
    await waitFor(() => expect(screen.getAllByTestId("assist-queue-item")).toHaveLength(2));
  });

  it("orders queue by priority sections (Ready to act, Needs input, collapsible Waiting) and supports sort and search", async () => {
    const waitingItem = todoItem("item_wait", "Supplier confirmation", {
      summary: "No answer yet from Ricardo",
      todos: [],
      created_at: "2026-11-25T12:00:00Z",
    });
    const readyItem = todoItem("item_ready", "RE: Ship date for PO 904", {
      kind: "reply",
      body: "We can ship Thursday.",
      todos: [],
      created_at: "2026-11-25T09:00:00Z",
    });
    const inputItem = todoItem("item_input", "You said: Send updated pricing sheet", {
      kind: "your_call",
      ask: "Send list price or contract price?",
      todos: [],
      created_at: "2026-11-25T11:00:00Z",
    });
    mocks.getAutopilotDrafts.mockResolvedValue({ items: [waitingItem, inputItem, readyItem] });
    mocks.getAssistJobs.mockResolvedValue({ jobs: [], status: "ok" });

    render(<AssistWorkbench linked={null} />);
    await waitFor(() => expect(screen.getByTestId("queue-section-ready")).toBeInTheDocument());
    expect(screen.getByTestId("queue-section-input")).toBeInTheDocument();
    expect(screen.getByTestId("queue-section-waiting")).toBeInTheDocument();

    // Ready leads first, waiting is collapsed by default when actionable work exists
    const visibleBeforeExpand = screen.getAllByTestId("assist-queue-item");
    expect(visibleBeforeExpand).toHaveLength(2);
    expect(visibleBeforeExpand[0]).toHaveTextContent("Ship date for PO 904");
    expect(visibleBeforeExpand[1]).toHaveTextContent("Send updated pricing sheet");

    // Expanding Waiting reveals the waiting row
    fireEvent.click(screen.getByRole("button", { name: /Waiting/ }));
    expect(screen.getAllByTestId("assist-queue-item")).toHaveLength(3);

    // Switching sort to Newest orders chronologically without sections
    fireEvent.change(screen.getByTestId("assist-queue-sort"), { target: { value: "newest" } });
    const newest = screen.getAllByTestId("assist-queue-item");
    expect(newest[0]).toHaveTextContent("Supplier confirmation");
  });

  it("shows an inline loading indicator under fast-loaded jobs while prepared drafts are still loading, and reveals deep-linked items", async () => {
    let resolveDrafts!: (d: DraftList) => void;
    mocks.getAutopilotDrafts.mockImplementation(() => new Promise<DraftList>((r) => (resolveDrafts = r)));
    mocks.getAssistJobs.mockResolvedValue({
      status: "ok",
      jobs: [
        {
          job_id: "j1",
          text: "Reply to all my teams messages",
          status: "finished",
          reason: "",
          message: "",
          progress: "",
          requested_at: "2026-11-24T20:00:00Z",
          summary: "Summary",
          checklist: [],
          found: "",
          not_found: "",
          nothing: "",
          planned: 0,
          unread: [],
          fallbacks: [],
          plan_reads: [],
          unread_reads: [],
          pieces: [],
        },
      ],
    });

    const other: PreparedItem = {
      ...todoItem("item_linked", "Linked colleague task"),
      note: "This is on Ross's list, not yours. Only you can see this.",
    };

    render(<AssistWorkbench linked="item_linked" />);
    await waitFor(() => expect(screen.getAllByTestId("assist-queue-item")).toHaveLength(1));
    expect(screen.getByTestId("assist-queue-loading")).toHaveTextContent("Loading prepared items...");

    await act(async () => {
      resolveDrafts({ items: [todoItem("item_mine", "My item"), other] });
      await Promise.resolve();
    });

    await waitFor(() => expect(screen.queryByTestId("assist-queue-loading")).toBeNull());
    await waitFor(() => expect(screen.getByTestId("assist-detail")).toHaveTextContent("Linked colleague task"));
  });

  it("supports collapsible New Assist job composer and instant hover quick-dismiss in the queue", async () => {
    const items = [todoItem("item_1", "First item"), todoItem("item_2", "Second item")];
    mocks.getAutopilotDrafts.mockResolvedValue({ items });
    mocks.getAssistJobs.mockResolvedValue({ jobs: [], status: "ok" });
    mocks.decideAutopilotItem.mockResolvedValue({});

    render(<AssistWorkbench linked={null} />);
    await waitFor(() => expect(screen.getAllByTestId("assist-queue-item")).toHaveLength(2));

    // Composer starts collapsed so the 2-pane inbox sits cleanly above the fold
    expect(screen.queryByTestId("give-job")).toBeNull();
    fireEvent.click(screen.getByTestId("assist-new-job-toggle"));
    expect(screen.getByTestId("give-job")).toBeInTheDocument();

    // Instant hover quick-dismiss removes the item optimistically
    const dismissButtons = screen.getAllByTestId("assist-quick-dismiss");
    fireEvent.click(dismissButtons[0]);
    expect(screen.getAllByTestId("assist-queue-item")).toHaveLength(1);
    expect(screen.getByTestId("assist-toast")).toHaveTextContent("Dismissed");
    await waitFor(() => expect(mocks.decideAutopilotItem).toHaveBeenCalledWith("item_1", { decision: "reject" }));
  });
});
