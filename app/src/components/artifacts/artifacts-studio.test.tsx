import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const routerPush = vi.fn();

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: routerPush, replace: vi.fn() }),
}));

const apiMocks = vi.hoisted(() => ({
  getAskChats: vi.fn(),
  getInsights: vi.fn(),
  getAssistJobs: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  getAskChats: apiMocks.getAskChats,
  getInsights: apiMocks.getInsights,
  getAssistJobs: apiMocks.getAssistJobs,
}));

vi.mock("@/components/ui/toolbar", () => ({
  Toolbar: ({ title, actions }: { title: string; actions?: React.ReactNode }) => (
    <header>
      <h1>{title}</h1>
      <div>{actions}</div>
    </header>
  ),
  ToolbarButton: ({ children, primary: _primary, ...props }: React.ButtonHTMLAttributes<HTMLButtonElement> & { primary?: boolean }) => (
    <button type="button" {...props}>
      {children}
    </button>
  ),
}));

import {
  ArtifactsStudio,
  buildCreationPrompt,
  inferChatArtifactType,
  isDeliverableChat,
  LOCAL_ARTIFACTS_KEY,
  PINNED_IDS_KEY,
  STUDIO_FORMATS,
} from "./artifacts-studio";

describe("ArtifactsStudio", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    window.localStorage.removeItem(LOCAL_ARTIFACTS_KEY);
    window.localStorage.removeItem(PINNED_IDS_KEY);
  });

  it("shows only real deliverables by default (saved insights, prepared assist jobs, deliverable chats) and hides random Q&A threads behind an opt-in toggle", async () => {
    apiMocks.getAskChats.mockResolvedValue({
      chats: [
        {
          conversation_id: "c-doc-1",
          title: "Q4 Supplier Quality Brief (.docx)",
          last_at: "2026-11-25T14:00:00Z",
          named: true,
          pinned: true,
          questions: 3,
          running: false,
        },
        {
          conversation_id: "c-sheet-2",
          title: "Open PO landed cost Excel tracker",
          last_at: "2026-11-24T12:00:00Z",
          named: false,
          pinned: false,
          questions: 1,
          running: false,
        },
        {
          conversation_id: "c-junk-3",
          title: "Who emailed about box 14?",
          last_at: "2026-11-23T10:00:00Z",
          named: false,
          pinned: false,
          questions: 1,
          running: false,
        },
      ],
    });
    apiMocks.getInsights.mockResolvedValue({
      items: [
        {
          id: "builtin-late",
          about: "Late purchase orders by week",
          group: "Operations",
          notify: "never",
          saved: false,
          last_run: "2026-11-25T08:00:00Z",
        },
        {
          id: "s42",
          about: "Monthly revenue by customer",
          question: "Monthly revenue by customer",
          group: "Sales",
          notify: "never",
          saved: true,
          last_run: "2026-11-25T09:00:00Z",
        },
      ],
    });
    apiMocks.getAssistJobs.mockResolvedValue({
      status: "ok",
      reason: "",
      jobs: [
        {
          job_id: "job-7",
          text: "Chase unconfirmed POs from Acme Packaging",
          summary: "Follow up on 4 unconfirmed Acme POs",
          status: "finished",
          requested_at: "2026-11-23T16:00:00Z",
          pieces: [{ n: 1, title: "Email Acme", status: "prepared" }],
        },
        {
          job_id: "job-empty",
          text: "No-op job",
          summary: "Nothing prepared",
          status: "finished",
          requested_at: "2026-11-22T16:00:00Z",
          pieces: [],
        },
      ],
    });

    render(<ArtifactsStudio />);

    expect(screen.getByTestId("create-deliverable-studio")).toBeInTheDocument();
    expect(screen.getByTestId("studio-format-sheets")).toBeInTheDocument();
    expect(screen.getByTestId("studio-format-docs")).toBeInTheDocument();
    expect(screen.getByTestId("studio-format-slides")).toBeInTheDocument();
    expect(screen.getByTestId("studio-format-charts")).toBeInTheDocument();
    expect(screen.getByTestId("studio-format-custom")).toBeInTheDocument();

    // 4 real deliverables: 2 deliverable chats + 1 saved insight + 1 assist job with prepared piece
    // Excludes unsaved catalog insight ("Late purchase orders by week"), empty assist job, and random Q&A chat
    const cards = await screen.findAllByTestId("artifact-card");
    expect(cards).toHaveLength(4);
    expect(screen.queryByText("Late purchase orders by week")).toBeNull();
    expect(screen.queryByText("Who emailed about box 14?")).toBeNull();
    expect(screen.queryByText("Nothing prepared")).toBeNull();

    // Opting into Ask threads reveals the random Q&A thread
    fireEvent.click(screen.getByTestId("artifacts-include-threads"));
    expect(screen.getAllByTestId("artifact-card")).toHaveLength(5);
    expect(screen.getByText("Who emailed about box 14?")).toBeInTheDocument();

    // Search filters live
    fireEvent.change(screen.getByTestId("artifacts-search"), { target: { value: "landed cost" } });
    expect(screen.getAllByTestId("artifact-card")).toHaveLength(1);
    expect(screen.getByText("Open PO landed cost Excel tracker")).toBeInTheDocument();

    // Switch to List view
    fireEvent.click(screen.getByTestId("artifacts-view-list"));
    expect(screen.getByTestId("artifacts-list")).toBeInTheDocument();
    expect(screen.getAllByTestId("artifact-row")).toHaveLength(1);
  });

  it("creates a deliverable from the prompt-first studio, supports SharePoint folder, pins locally, and routes to Ask", async () => {
    apiMocks.getAskChats.mockResolvedValue({ chats: [] });
    apiMocks.getInsights.mockResolvedValue({ items: [] });
    apiMocks.getAssistJobs.mockResolvedValue({ status: "ok", reason: "", jobs: [] });

    render(<ArtifactsStudio />);
    await screen.findByTestId("artifacts-empty");

    fireEvent.click(screen.getByTestId("studio-format-slides"));
    fireEvent.change(screen.getByTestId("artifact-domain-select"), { target: { value: "Finance" } });
    fireEvent.change(screen.getByTestId("artifact-prompt-input"), {
      target: { value: "Q4 Margin Review comparing Q3 vs Q4 by customer" },
    });
    fireEvent.click(screen.getByTestId("artifact-sharepoint-toggle"));
    expect(screen.getByTestId("artifact-sharepoint-folder")).toHaveValue("Shared Documents/Finance");

    fireEvent.click(screen.getByTestId("artifact-create-submit"));

    await waitFor(() => expect(routerPush).toHaveBeenCalledTimes(1));
    const pushedUrl = routerPush.mock.calls[0][0] as string;
    expect(pushedUrl).toContain("/ask?q=");
    const decoded = decodeURIComponent(pushedUrl.split("?q=")[1]);
    expect(decoded).toContain(
      "Create a PowerPoint presentation (.pptx) summarizing Q4 Margin Review comparing Q3 vs Q4 by customer (Finance team)",
    );
    expect(decoded).toContain('Prepare a SharePoint upload draft for folder "Shared Documents/Finance"');

    const stored = JSON.parse(window.localStorage.getItem(LOCAL_ARTIFACTS_KEY) ?? "[]");
    expect(stored).toHaveLength(1);
    expect(stored[0].title).toBe("Q4 Margin Review comparing Q3 vs Q4 by customer");
    expect(stored[0].type).toBe("Slides");
  });

  it("gracefully renders starter state when all API endpoints fail", async () => {
    apiMocks.getAskChats.mockRejectedValue(new Error("offline"));
    apiMocks.getInsights.mockRejectedValue(new Error("offline"));
    apiMocks.getAssistJobs.mockRejectedValue(new Error("offline"));

    render(<ArtifactsStudio />);
    expect(await screen.findByTestId("artifacts-empty")).toBeInTheDocument();
    expect(screen.getByText("No deliverables saved here yet")).toBeInTheDocument();
  });

  it("classifies deliverable chats, infers artifact types, and builds structured prompts cleanly", () => {
    expect(isDeliverableChat({ title: "Open PO landed cost tracker.xlsx" })).toBe(true);
    expect(isDeliverableChat({ title: "Q4 Supplier Brief" })).toBe(true);
    expect(isDeliverableChat({ title: "What did Bob say yesterday?" })).toBe(false);

    expect(inferChatArtifactType("Create a pptx deck for Q4")).toBe("Slides");
    expect(inferChatArtifactType("Build Excel sheet for margins")).toBe("Sheet");
    expect(inferChatArtifactType("Show monthly revenue chart")).toBe("Chart");
    expect(inferChatArtifactType("Weekly workflow run")).toBe("Workflow");
    expect(inferChatArtifactType("Draft supplier memo")).toBe("Doc");

    const sheetFormat = STUDIO_FORMATS.find((f) => f.id === "sheets")!;
    expect(buildCreationPrompt(sheetFormat, "PO 90412 cost tracker", "Purchasing")).toBe(
      "Build an Excel spreadsheet (.xlsx) with formulas for PO 90412 cost tracker (Purchasing team)",
    );
    expect(
      buildCreationPrompt(sheetFormat, "PO 90412 cost tracker", "Purchasing", {
        enabled: true,
        folder: "Shared Documents/Purchasing",
      }),
    ).toContain('Prepare a SharePoint upload draft for folder "Shared Documents/Purchasing"');
  });

  it("paginates deliverables at 12 items with Show more and filters by format", async () => {
    apiMocks.getAskChats.mockResolvedValue({
      chats: Array.from({ length: 15 }, (_, i) => ({
        conversation_id: `c-${i}`,
        title: i < 10 ? `Supplier memo ${i}` : `Landed cost Excel tracker ${i}`,
        last_at: `2026-11-${String(25 - (i % 20)).padStart(2, "0")}T12:00:00Z`,
        named: true,
        pinned: false,
        questions: 2,
        running: false,
      })),
    });
    apiMocks.getInsights.mockResolvedValue({ items: [] });
    apiMocks.getAssistJobs.mockResolvedValue({ status: "ok", reason: "", jobs: [] });

    render(<ArtifactsStudio />);
    expect(await screen.findAllByTestId("artifact-card")).toHaveLength(12);
    expect(screen.getByTestId("artifacts-show-more")).toHaveTextContent("Show more (3 remaining)");

    fireEvent.click(screen.getByTestId("artifacts-show-more"));
    expect(screen.getAllByTestId("artifact-card")).toHaveLength(15);

    fireEvent.click(screen.getByTestId("artifacts-format-sheet"));
    expect(screen.getAllByTestId("artifact-card")).toHaveLength(5);
  });
});
