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
  getSharePointFolders: vi.fn(),
  uploadArtifactToSharePoint: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  getAskChats: apiMocks.getAskChats,
  getInsights: apiMocks.getInsights,
  getAssistJobs: apiMocks.getAssistJobs,
  getSharePointFolders: apiMocks.getSharePointFolders,
  uploadArtifactToSharePoint: apiMocks.uploadArtifactToSharePoint,
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
  PINNED_IDS_KEY,
  STUDIO_FORMATS,
} from "./artifacts-studio";

const MOCK_SP_FOLDERS = {
  folders: [
    {
      path: "CM Operations Files/2026 Reports",
      label: "2026 Reports",
      drive_id: "b!drive-ops",
      parent_id: "folder-1",
      site: "Operations",
      file_count: 24,
      source: "postgres",
    },
    {
      path: "Shared Documents/Finance",
      label: "Finance",
      drive_id: "b!drive-fin",
      parent_id: "root",
      site: "Company Library",
      file_count: 8,
      source: "postgres",
    },
  ],
};

describe("ArtifactsStudio", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    window.localStorage.removeItem("plx.artifacts.local.v1");
    window.localStorage.removeItem(PINNED_IDS_KEY);
    apiMocks.getSharePointFolders.mockResolvedValue(MOCK_SP_FOLDERS);
  });

  it("renders the 5 editorial visual preview cards and shows only real deliverables by default", async () => {
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
    expect(screen.getByTestId("deliverable-showcase")).toBeInTheDocument();

    // All 5 bespoke SVG editorial preview cards render
    for (const id of ["sheets", "docs", "slides", "charts", "custom"] as const) {
      const card = screen.getByTestId(`showcase-card-${id}`);
      expect(card).toBeInTheDocument();
      expect(card.querySelector("svg")).not.toBeNull();
    }

    // Clicking a showcase card selects that format directly (no redundant second format bar)
    expect(screen.queryByTestId("artifact-format-pills")).toBeNull();
    fireEvent.click(screen.getByTestId("showcase-card-docs"));
    expect(screen.getByTestId("showcase-card-docs")).toHaveAttribute("data-selected", "true");
    expect(screen.getByTestId("showcase-card-docs")).toHaveAttribute("aria-checked", "true");
    expect(screen.getByTestId("studio-active-format")).toHaveTextContent("Word Brief / SOP");

    // 4 real deliverables: 2 deliverable chats + 1 saved insight + 1 assist job with prepared piece
    const cards = await screen.findAllByTestId("artifact-card");
    expect(cards).toHaveLength(4);
    expect(cards[0].querySelector("svg")).not.toBeNull();
    expect(screen.queryByText("Late purchase orders by week")).toBeNull();
    expect(screen.queryByText("Who emailed about box 14?")).toBeNull();
    expect(screen.queryByText("Nothing prepared")).toBeNull();

    // Opting into Ask threads reveals the random Q&A thread (cleaned via cleanChatTitle)
    fireEvent.click(screen.getByTestId("artifacts-include-threads"));
    expect(screen.getAllByTestId("artifact-card")).toHaveLength(5);
    expect(screen.getByText("Who Emailed About Box 14")).toBeInTheDocument();

    // Search filters live
    fireEvent.change(screen.getByTestId("artifacts-search"), { target: { value: "landed cost" } });
    expect(screen.getAllByTestId("artifact-card")).toHaveLength(1);
    expect(screen.getByText("Open PO Landed Cost Excel Tracker")).toBeInTheDocument();

    // Switch to List view
    fireEvent.click(screen.getByTestId("artifacts-view-list"));
    expect(screen.getByTestId("artifacts-list")).toBeInTheDocument();
    expect(screen.getAllByTestId("artifact-row")).toHaveLength(1);
  });

  it("routes directly to Ask with real SharePoint folder context without creating fake localStorage stubs", async () => {
    apiMocks.getAskChats.mockResolvedValue({ chats: [] });
    apiMocks.getInsights.mockResolvedValue({ items: [] });
    apiMocks.getAssistJobs.mockResolvedValue({ status: "ok", reason: "", jobs: [] });

    render(<ArtifactsStudio />);
    await screen.findByTestId("artifacts-empty");

    fireEvent.click(screen.getByTestId("showcase-card-slides"));
    fireEvent.change(screen.getByTestId("artifact-domain-select"), { target: { value: "Finance" } });
    fireEvent.change(screen.getByTestId("artifact-prompt-input"), {
      target: { value: "Q4 Margin Review comparing Q3 vs Q4 by customer" },
    });
    fireEvent.click(screen.getByTestId("artifact-sharepoint-toggle"));

    // Known SharePoint folders fetched from backend appear as clickable chips
    expect(await screen.findByText("CM Operations Files/2026 Reports")).toBeInTheDocument();
    fireEvent.click(screen.getByText("CM Operations Files/2026 Reports"));
    expect(screen.getByTestId("artifact-sharepoint-folder")).toHaveValue("CM Operations Files/2026 Reports");

    fireEvent.click(screen.getByTestId("artifact-create-submit"));

    await waitFor(() => expect(routerPush).toHaveBeenCalledTimes(1));
    const pushedUrl = routerPush.mock.calls[0][0] as string;
    expect(pushedUrl).toContain("/ask?q=");
    const decoded = decodeURIComponent(pushedUrl.split("?q=")[1]);
    expect(decoded).toContain(
      "Create a PowerPoint presentation (.pptx) summarizing Q4 Margin Review comparing Q3 vs Q4 by customer (Finance team)",
    );
    expect(decoded).toContain('Prepare a SharePoint upload draft for folder "CM Operations Files/2026 Reports"');

    // No fake localStorage entries written
    expect(window.localStorage.getItem("plx.artifacts.local.v1")).toBeNull();
  });

  it("opens Save to SharePoint popover on a gallery card and queues a durable approval draft", async () => {
    apiMocks.getAskChats.mockResolvedValue({
      chats: [
        {
          conversation_id: "c-sheet-9",
          title: "Open PO Landed Cost Tracker.xlsx",
          last_at: "2026-11-25T14:00:00Z",
          named: true,
          pinned: false,
          questions: 2,
          running: false,
        },
      ],
    });
    apiMocks.getInsights.mockResolvedValue({ items: [] });
    apiMocks.getAssistJobs.mockResolvedValue({ status: "ok", reason: "", jobs: [] });
    apiMocks.uploadArtifactToSharePoint.mockResolvedValue({
      status: "approval_queued",
      draft_id: "sp_abc123456789",
      upload_id: "up_abc123456789",
      file_id: "f_0123456789abcdef01234567",
      folder_path: "CM Operations Files/2026 Reports",
      create_folder_if_missing: true,
      filename: "Open PO Landed Cost Tracker.xlsx",
      size_bytes: 6420,
      download_url: "/api/instruct/files/f_0123456789abcdef01234567",
      assist_url: "/assist",
      message: 'Prepared Open PO Landed Cost Tracker.xlsx for SharePoint folder "CM Operations Files/2026 Reports".',
    });

    render(<ArtifactsStudio />);
    await screen.findAllByTestId("artifact-card");

    fireEvent.click(screen.getByTestId("artifact-save-sharepoint"));
    expect(screen.getByTestId("sharepoint-upload-popover")).toBeInTheDocument();
    expect(screen.getByTestId("sp-upload-folder-input")).toHaveValue("CM Operations Files/2026 Reports");

    fireEvent.click(screen.getByTestId("sp-upload-submit"));

    await waitFor(() => expect(apiMocks.uploadArtifactToSharePoint).toHaveBeenCalledTimes(1));
    expect(apiMocks.uploadArtifactToSharePoint).toHaveBeenCalledWith(
      expect.objectContaining({
        title: "Open PO Landed Cost Tracker.xlsx",
        format: "xlsx",
        folder_path: "CM Operations Files/2026 Reports",
        create_folder_if_missing: true,
      }),
    );

    expect(await screen.findByTestId("sp-upload-result")).toBeInTheDocument();
    expect(screen.getByText("Queued for SharePoint approval")).toBeInTheDocument();
    expect(screen.getByTestId("sp-upload-download")).toHaveAttribute(
      "href",
      "/api/instruct/files/f_0123456789abcdef01234567",
    );
    expect(screen.getByTestId("sp-upload-approve-assist")).toHaveAttribute("href", "/assist");
  });

  it("gracefully renders starter state when all API endpoints fail", async () => {
    apiMocks.getAskChats.mockRejectedValue(new Error("offline"));
    apiMocks.getInsights.mockRejectedValue(new Error("offline"));
    apiMocks.getAssistJobs.mockRejectedValue(new Error("offline"));
    apiMocks.getSharePointFolders.mockRejectedValue(new Error("offline"));

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

  it("cleans raw Ask deliverable prompts into crisp titles via cleanChatTitle", async () => {
    apiMocks.getAskChats.mockResolvedValue({
      chats: [
        {
          conversation_id: "c-soapbox",
          title: "chart and give me all info about soapbox shipments",
          last_at: "2026-11-25T14:00:00Z",
          named: false,
          pinned: false,
          questions: 2,
          running: false,
        },
      ],
    });
    apiMocks.getInsights.mockResolvedValue({ items: [] });
    apiMocks.getAssistJobs.mockResolvedValue({ status: "ok", reason: "", jobs: [] });

    render(<ArtifactsStudio />);
    await screen.findAllByTestId("artifact-card");
    expect(screen.getByText("Soapbox Shipments Overview")).toBeInTheDocument();
  });
});
