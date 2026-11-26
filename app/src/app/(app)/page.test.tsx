import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { HOME_PREFS_KEY } from "@/components/home/home-prefs";
import type { HomeNeedItem, HomeNeeds } from "@/lib/types";
import { forgetKeptReads } from "@/lib/use-read";

const nav = vi.hoisted(() => ({
  search: "",
  push: vi.fn(),
  replace: vi.fn(),
}));
const api = vi.hoisted(() => ({
  needs: vi.fn(),
  decide: vi.fn(),
  watcher: vi.fn(),
  homeState: vi.fn(),
  close: vi.fn(),
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: nav.push, replace: nav.replace }),
  useSearchParams: () => new URLSearchParams(nav.search),
  usePathname: () => "/",
}));
vi.mock("@/lib/api", async (orig) => ({
  ...(await orig<typeof import("@/lib/api")>()),
  getHomeNeeds: (...args: unknown[]) => api.needs(...args),
  getUpcomingMeetings: () => Promise.resolve({ meetings: [] }),
  decideAutopilotItem: (...args: unknown[]) => api.decide(...args),
  setWatcherItemState: (...args: unknown[]) => api.watcher(...args),
  setHomeItemState: (...args: unknown[]) => api.homeState(...args),
  getPoClosure: () => Promise.resolve({ closed: false }),
  closePo: (...args: unknown[]) => api.close(...args),
}));

import HomePage from "./page";
import { publishNeedsCount } from "@/components/shell";

function item(n: number, over: Partial<HomeNeedItem> = {}): HomeNeedItem {
  return {
    id: `n${n}`,
    kind: "task",
    source: "planner",
    source_label: "Planner",
    title: `Need ${n}`,
    line: `Line for need ${n}`,
    action: "do",
    action_label: "Do it",
    href: `/task/${n}`,
    rank: n,
    rank_reasons: [],
    state: "open",
    still_open: false,
    draft_ready: false,
    basis_at: "2026-11-25T12:00:00Z",
    ...over,
  };
}

function needs(items: HomeNeedItem[], over: Partial<HomeNeeds> = {}): HomeNeeds {
  return {
    status: "ok",
    total: items.length,
    items,
    older_total: over.older_items?.length ?? 0,
    older_items: [],
    older_after_days: 90,
    sources: [],
    kinds: [],
    people: [],
    parts: [],
    as_of: "2026-11-25T14:00:00Z",
    ...over,
  };
}

beforeEach(() => {
  forgetKeptReads();
  window.localStorage.clear();
  nav.search = "";
  nav.push.mockReset();
  nav.replace.mockReset();
  api.needs.mockReset();
  api.decide.mockReset();
  api.watcher.mockReset();
  api.homeState.mockReset();
  api.close.mockReset();
  publishNeedsCount(null, true);
  window.history.replaceState(null, "", "/");
});

describe("Executive Home workspace: command bar, spotlight + action stream, schedule rail, and customization", () => {
  const sampleItems = (): HomeNeedItem[] => [
    item(1, {
      id: "matter:r1",
      matter_id: "r1",
      kind: "reply",
      source: "mail",
      source_label: "Mail",
      person: "Maya",
      title: "Re: packing slip",
      line: "Confirm carton count before pickup.",
      action: "reply",
      action_label: "Reply",
      href: "/assist?item=r1",
    }),
    item(2, {
      id: "matter:w1",
      matter_id: "w1",
      kind: "waiting",
      source: "teams",
      source_label: "Teams",
      person: "Elena",
      title: "No answer yet from Elena",
      line: "Elena: Portal write endpoints and key in staging (no typical reply time on record yet)\nOnly you can see this.",
      basis_at: "2026-11-18T12:00:00Z",
      action: "check",
      action_label: "Check",
      href: "/assist?item=w1",
    }),
    item(3, {
      id: "alert:a1",
      kind: "alert",
      source: "alerts",
      source_label: "bc",
      title: "Supplier ETA slipped",
      line: "Shipment moved out by 4 days.",
      action: "decide",
      action_label: "Decide",
      href: "/po/90500",
    }),
    item(4, {
      id: "matter:qc1",
      matter_id: "qc1",
      kind: "alert",
      source: "alerts",
      source_label: "Alerts",
      person: "Tiger Hospitality LLC",
      title: "QC hold or fail: Lot BC-25295D (AA-NA650) is rejected; it holds up 1 open order.",
      line: "9 Nov. QC hold or fail.\nOnly you can see this.",
      due_at: "2026-11-24T12:00:00Z",
      event_type: "qc_hold",
      alert_kind: "quality_hold",
      section: "holds",
      badge: "QC hold",
      action: "decide",
      action_label: "Decide",
      href: "/object/Batch/Batch%3Abc%3ABC-25295D",
    }),
    item(5, {
      id: "late:po1",
      kind: "late_po",
      source: "pos",
      source_label: "POs",
      person: "Acme Supply",
      title: "PO 90412",
      line: "Needed 12 Nov.",
      days_late: 13,
      action: "check",
      action_label: "Check",
      href: "/po/90412",
    }),
  ];

  it("renders compact masthead, Command Ask bar, zero 4-column hero KPI strip, Spotlight + Action Stream, properly capitalized BC source, Today rail, and non-zero Focus card", async () => {
    api.needs.mockResolvedValue(needs(sampleItems()));
    render(<HomePage />);

    await screen.findByTestId("home-grouped-view");
    expect(screen.getByTestId("today-schedule-card")).toBeInTheDocument();
    expect(screen.getByTestId("home-focus-card")).toBeInTheDocument();

    // Banned 4-column hero metric strip and old empty Watching box are gone.
    expect(screen.queryByTestId("home-pulse-strip")).toBeNull();
    expect(screen.queryByTestId("home-watching-card")).toBeNull();

    // Unified Command / Ask-or-Jump bar routes Enter / Ask to /ask?q=...
    const cmdBar = screen.getByTestId("home-command-bar");
    const cmdInput = within(cmdBar).getByTestId("home-command-input");
    fireEvent.change(cmdInput, { target: { value: "open orders for Acme" } });
    fireEvent.submit(cmdInput.closest("form")!);
    expect(nav.push).toHaveBeenCalledWith("/ask?q=open%20orders%20for%20Acme");

    // Top 2 highest-urgency items appear in Priority Spotlight; remaining non-hold items appear in Action Stream.
    const spotlight = screen.getByTestId("home-spotlight");
    expect(within(spotlight).getAllByTestId("spotlight-card")).toHaveLength(2);
    expect(spotlight).toHaveTextContent("Re: packing slip");
    expect(spotlight).toHaveTextContent("Supplier ETA slipped");

    // Source label "bc" is always capitalized as "BC".
    const sources = screen.getAllByTestId("need-source").map((el) => el.textContent);
    expect(sources).toContain("BC");
    expect(sources).not.toContain("bc");

    // Header total, "All" pill, and sum of section filter pills are mathematically identical (5 = 1 + 3 + 1).
    expect(screen.getByTestId("needs-total")).toHaveTextContent("5");
    const strip = screen.getByTestId("section-filters");
    expect(within(strip).getByTestId("filter-section-all")).toHaveTextContent("All5");
    expect(within(strip).getByTestId("filter-section-replies")).toHaveTextContent("Replies1");
    expect(within(strip).getByTestId("filter-section-holds_alerts")).toHaveTextContent("Alerts3");
    expect(within(strip).getByTestId("filter-section-waiting")).toHaveTextContent("Waiting1");

    // Clicking "Waiting" filters directly to Elena's item with promoted topic headline.
    fireEvent.click(within(strip).getByTestId("filter-section-waiting"));
    await waitFor(() => expect(screen.getAllByTestId("need-row")).toHaveLength(1));
    expect(screen.getByTestId("needs-total")).toHaveTextContent("1");
    expect(screen.getByTestId("need-row")).toHaveTextContent("Portal write endpoints and key in staging");
    expect(screen.getByTestId("need-row")).toHaveTextContent("Waiting on Elena");

    // Clicking the active filter again restores all 5 items.
    fireEvent.click(within(strip).getByTestId("filter-section-waiting"));
    await waitFor(() => expect(screen.getAllByTestId("need-row")).toHaveLength(5));
    expect(screen.getByTestId("needs-total")).toHaveTextContent("5");

    // Right Focus card shows only non-zero counters (no 0/0/0/0 empty table).
    const attentionList = screen.getByTestId("home-attention-list");
    expect(attentionList).toHaveTextContent("Overdue items");
    expect(attentionList).toHaveTextContent("Active QC holds");
    expect(attentionList).not.toHaveTextContent("Drafts ready to send");
  });

  it("hides zero counters cleanly in the right focus rail when all operational queues are empty", async () => {
    api.needs.mockResolvedValue(needs([]));
    render(<HomePage />);

    await screen.findByTestId("home-empty");
    const focusCard = screen.getByTestId("home-focus-card");
    expect(focusCard).toHaveTextContent("Quick access");
    expect(within(focusCard).queryByTestId("home-attention-list")).toBeNull();
    expect(focusCard).toHaveTextContent("Assist queue");
    expect(focusCard).toHaveTextContent("View dashboard");
    expect(focusCard).toHaveTextContent("Weekly review");
  });

  it("caps long lists to top 7 scannable rows by default with a clean Show more expander", async () => {
    const manyTasks = Array.from({ length: 12 }, (_, idx) =>
      item(idx + 1, {
        id: `t${idx + 1}`,
        kind: "task",
        title: `Follow up task ${idx + 1}`,
        line: `Action item details ${idx + 1}`,
      }),
    );
    api.needs.mockResolvedValue(needs(manyTasks));
    render(<HomePage />);

    await waitFor(() => expect(screen.getAllByTestId("need-row")).toHaveLength(7));
    expect(screen.getByTestId("needs-paging")).toHaveTextContent("Showing 7 of 12");

    fireEvent.click(within(screen.getByTestId("needs-paging")).getByRole("button", { name: /Show 5 more/i }));
    await waitFor(() => expect(screen.getAllByTestId("need-row")).toHaveLength(12));
  });

  it("defensively deduplicates overlapping notifications and shows (+1 update)", async () => {
    api.needs.mockResolvedValue(
      needs([
        item(1, {
          id: "matter:w1",
          matter_id: "w1",
          kind: "waiting",
          source: "teams",
          source_label: "Teams",
          person: "Elena",
          title: "No answer yet from Elena",
          line: "Elena: Portal write endpoints and key in staging (no typical reply time on record yet)",
          basis_at: "2026-11-18T12:00:00Z",
          href: "/assist?item=w1",
        }),
        item(2, {
          id: "matter:w2",
          matter_id: "w2",
          kind: "waiting",
          source: "teams",
          source_label: "Teams",
          person: "Elena",
          title: "No answer yet from Elena",
          line: "Elena: Portal write endpoints and key in staging environment",
          basis_at: "2026-11-23T12:00:00Z",
          href: "/assist?item=w2",
        }),
        item(3, {
          id: "matter:qc1",
          matter_id: "qc1",
          kind: "alert",
          source: "alerts",
          source_label: "Alerts",
          person: "Tiger Hospitality LLC",
          title: "QC hold or fail: Lot BC-25295D (AA-NA650) is rejected; it holds up 1 open order.",
          line: "9 Nov. QC hold or fail.",
          object_id: "Batch:bc:BC-25295D",
          section: "holds",
          badge: "QC hold",
          href: "/object/Batch/Batch%3Abc%3ABC-25295D",
        }),
        item(4, {
          id: "alert:qc2",
          kind: "alert",
          source: "alerts",
          source_label: "Alerts",
          title: "Lot BC-25295D rejected (AA-NA650)",
          line: "1 open order affected",
          object_id: "Batch:bc:BC-25295D",
          section: "holds",
          badge: "QC hold",
          href: "/object/Batch/Batch%3Abc%3ABC-25295D",
        }),
      ]),
    );
    render(<HomePage />);

    // 4 raw items collapse to 2 unique notifications, each showing (+1 update).
    await waitFor(() => expect(screen.getAllByTestId("need-row")).toHaveLength(2));
    const badges = screen.getAllByTestId("need-merged-count");
    expect(badges).toHaveLength(2);
    expect(badges[0]).toHaveTextContent("(+1 update)");
    expect(badges[1]).toHaveTextContent("(+1 update)");
  });

  it("defensively collapses same-second promise + conversation pairs, preferring the richer planned item", async () => {
    api.needs.mockResolvedValue(
      needs([
        item(1, {
          id: "matter:vp1",
          matter_id: "vp1",
          kind: "task",
          source: "teams",
          source_label: "Teams",
          person: "Jordan",
          title: "You said: send the 8oz template to Greg",
          line: "",
          action_label: "Commitment",
          plan_status: "none",
          basis_at: "2026-11-25T15:18:33Z",
          href: "/assist?item=vp1",
        }),
        item(2, {
          id: "matter:vc1",
          matter_id: "vc1",
          kind: "reply",
          source: "teams",
          source_label: "Teams",
          person: "Greg",
          title: "Jordan has the 8oz template and will send it to Greg.",
          line: "",
          action_label: "Reply",
          plan_status: "planned",
          draft_ready: true,
          basis_at: "2026-11-25T15:18:33Z",
          href: "/assist?item=vc1",
        }),
        item(3, {
          id: "matter:vp2",
          matter_id: "vp2",
          kind: "task",
          source: "teams",
          source_label: "Teams",
          person: "Jordan",
          title: "You said: send his and Greg's lunch order",
          line: "",
          action_label: "Commitment",
          plan_status: "none",
          basis_at: "2026-11-25T12:37:06Z",
          href: "/assist?item=vp2",
        }),
        item(4, {
          id: "matter:vc2",
          matter_id: "vc2",
          kind: "task",
          source: "teams",
          source_label: "Teams",
          person: "Giulia",
          title: "Marco says he is trying to send Giulia his and Greg's lunch orders",
          line: "",
          action_label: "Do it",
          plan_status: "planned",
          basis_at: "2026-11-25T12:37:06Z",
          href: "/assist?item=vc2",
        }),
      ]),
    );
    render(<HomePage />);

    await waitFor(() => expect(screen.getAllByTestId("need-row")).toHaveLength(2));
    const rows = screen.getAllByTestId("need-row");
    expect(rows[0]).toHaveTextContent("Jordan has the 8oz template and will send it to Greg.");
    expect(rows[1]).toHaveTextContent("Marco says he is trying to send Giulia his and Greg's lunch orders");
  });

  it("toggles Compact density and visible sections, persisting Customize in localStorage", async () => {
    api.needs.mockResolvedValue(needs(sampleItems()));
    const view = render(<HomePage />);

    await screen.findByTestId("home-grouped-view");
    expect(screen.getAllByTestId("need-row")).toHaveLength(5);

    // Open Customize popover, switch to Compact density, and hide "Waiting on others".
    fireEvent.click(screen.getAllByTestId("home-customize-trigger")[0]);
    const popover = await screen.findByTestId("home-customize-popover");
    fireEvent.click(within(popover).getByTestId("popover-density-compact"));
    fireEvent.click(within(popover).getByTestId("toggle-section-waiting"));
    await waitFor(() => expect(screen.getAllByTestId("need-row")).toHaveLength(4));
    expect(screen.getByTestId("home-hidden-sections-note")).toHaveTextContent("1 section is hidden");

    // Verify persistence in localStorage across remounts.
    const stored = JSON.parse(window.localStorage.getItem(HOME_PREFS_KEY) || "{}");
    expect(stored.density).toBe("compact");
    expect(stored.visibleSections.waiting).toBe(false);

    view.unmount();
    render(<HomePage />);
    await screen.findByTestId("home-grouped-view");
    expect(screen.getAllByTestId("need-row")).toHaveLength(4);

    // Reset restores comfortable density and all sections.
    fireEvent.click(screen.getAllByTestId("home-customize-trigger")[0]);
    fireEvent.click(screen.getByTestId("home-customize-reset"));
    await waitFor(() => expect(screen.getAllByTestId("need-row")).toHaveLength(5));
  });

  it("shows crystal-clear plain-English text for waiting, commitments, and QC holds with zero em dashes", async () => {
    api.needs.mockResolvedValue(
      needs([
        ...sampleItems(),
        item(6, {
          id: "matter:c1",
          matter_id: "c1",
          kind: "task",
          source: "teams",
          source_label: "Teams",
          title: "You said: renew the staging-host-ops Portal access before Dec 14, 2026",
          line: "renew the staging-host-ops Portal access before Dec 14, 2026 (nothing in your mail says how long this usually takes: when should it be looked for?)",
          due_at: "2026-12-14T15:00:00Z",
          action: "do",
          action_label: "Do it",
          href: "/assist?item=c1",
        }),
      ]),
    );
    const { container } = render(<HomePage />);

    await waitFor(() => expect(screen.getAllByTestId("need-row")).toHaveLength(6));

    expect(screen.queryByText(/Private items marked with lock/i)).toBeNull();

    // Waiting item: topic promoted to headline, "Waiting on Elena" and "Teams" in metadata, no boilerplate.
    const allRows = screen.getAllByTestId("need-row");
    const waitingRow = allRows.find((el) => el.textContent?.includes("Portal write endpoints and key in staging"));
    expect(waitingRow).toBeTruthy();
    expect(waitingRow!).toHaveTextContent("Waiting on Elena");
    expect(waitingRow!).toHaveTextContent("Teams");
    expect(waitingRow!).not.toHaveTextContent("No answer yet from Elena");
    expect(waitingRow!).not.toHaveTextContent("no typical reply time");
    expect(waitingRow!).not.toHaveTextContent("Elena:");

    // Commitment item: crisp headline, "Commitment", "Due 14 Dec", "Teams" metadata, zero echoed subtitle.
    const commitmentRow = allRows.find((el) => el.textContent?.includes("staging-host-ops"));
    expect(commitmentRow).toBeTruthy();
    expect(commitmentRow!.textContent).toContain("Renew staging-host-ops Portal access");
    expect(commitmentRow!.textContent).toContain("Commitment");
    expect(commitmentRow!.textContent).toContain("Due 14 Dec");
    expect(commitmentRow!.textContent).toContain("Teams");
    expect(commitmentRow!.textContent).not.toContain("You said:");
    expect(commitmentRow!.textContent).not.toContain("nothing in your mail");
    expect(commitmentRow!.textContent!.split("staging-host-ops").length - 1).toBe(1);

    // QC Hold item: "Lot BC-25295D rejected (AA-NA650)" + "1 open order affected" + "Tiger Hospitality LLC".
    const holdRow = within(screen.getByTestId("holds-list")).getByTestId("need-row");
    expect(holdRow).toHaveTextContent("Lot BC-25295D rejected (AA-NA650)");
    expect(holdRow).toHaveTextContent("1 open order affected");
    expect(holdRow).toHaveTextContent("Tiger Hospitality LLC");
    expect(holdRow).toHaveTextContent("QC hold");
    expect(holdRow).not.toHaveTextContent("QC hold or fail");

    // Zero em dashes anywhere on the page.
    expect(container.textContent).not.toContain("\u2014");
  });

  it("supports universal 1-click Done and Snooze with Liquid Glass Undo on every row via setHomeItemState", async () => {
    api.homeState.mockResolvedValue({ item_id: "late:po1", state: "closed" });
    api.needs.mockResolvedValue(
      needs([
        item(1, {
          id: "matter:r1",
          matter_id: "r1",
          kind: "reply",
          source: "mail",
          source_label: "Mail",
          person: "Maya",
          title: "Re: packing slip",
          line: "Confirm carton count.",
          action: "reply",
          action_label: "Reply",
          href: "/assist?item=r1",
        }),
        item(2, {
          id: "late:po1",
          kind: "late_po",
          source: "pos",
          source_label: "POs",
          person: "Acme Supply",
          title: "PO 90412",
          line: "Needed 12 Nov.",
          days_late: 13,
          action: "check",
          action_label: "Check",
          href: "/po/90412",
        }),
      ]),
    );
    render(<HomePage />);

    await waitFor(() => expect(screen.getAllByTestId("need-row")).toHaveLength(2));

    expect(screen.getAllByTestId("row-quick-open")[0]).toHaveAttribute("href", "/assist?item=r1");
    expect(screen.getAllByTestId("row-quick-ask")[0].getAttribute("href")).toContain("/ask?q=");
    expect(screen.getAllByRole("button", { name: "Close item" })).toHaveLength(2);
    expect(screen.getAllByRole("button", { name: "Snooze 1 day" })).toHaveLength(2);

    // 1-click Snooze optimistically removes item and calls setHomeItemState("matter:r1", "snoozed", until).
    fireEvent.click(screen.getAllByTestId("row-quick-snooze")[0]);
    await waitFor(() =>
      expect(api.homeState).toHaveBeenCalledWith("matter:r1", "snoozed", expect.any(String)),
    );
    await waitFor(() => expect(screen.getAllByTestId("need-row")).toHaveLength(1));
    expect(screen.getByTestId("home-action-status")).toHaveTextContent("Snoozed Re: packing slip for 1 day");

    // 1-click Done on the non-matter late_po row optimistically removes it and calls setHomeItemState("late:po1", "closed").
    fireEvent.click(screen.getByTestId("row-quick-dismiss"));
    await waitFor(() => expect(api.homeState).toHaveBeenCalledWith("late:po1", "closed", undefined));
    await waitFor(() => expect(screen.queryAllByTestId("need-row")).toHaveLength(0));
    expect(screen.getByTestId("home-action-status")).toHaveTextContent("Closed PO 90412");

    // Clicking Undo restores the item immediately even if a background refresh returns without it, and calls setHomeItemState("late:po1", "open").
    api.needs.mockResolvedValueOnce(needs([]));
    fireEvent.click(screen.getByTestId("home-action-undo"));
    await waitFor(() => expect(api.homeState).toHaveBeenCalledWith("late:po1", "open"));
    await waitFor(() => expect(screen.getAllByTestId("need-row")).toHaveLength(1));
    expect(screen.getByTestId("need-row")).toHaveTextContent("PO 90412");
    expect(screen.getByTestId("needs-total")).toHaveTextContent("1");
  });
});
