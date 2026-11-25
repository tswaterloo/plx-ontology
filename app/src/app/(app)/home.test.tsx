import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { HomeNeedItem, HomeNeeds } from "@/lib/types";

const nav = vi.hoisted(() => ({
  search: "",
  push: vi.fn(),
  replace: vi.fn(),
}));
const api = vi.hoisted(() => ({
  needs: vi.fn(),
  decide: vi.fn(),
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
  decideAutopilotItem: (...args: unknown[]) => api.decide(...args),
  getPoClosure: () => Promise.resolve({ closed: false }),
  closePo: (...args: unknown[]) => api.close(...args),
}));

import HomePage from "./page";
import { navSectionsFor, publishNeedsCount, useNeedsCount } from "@/components/shell";

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
    rank_reasons: ["newest open work"],
    state: "open",
    still_open: false,
    draft_ready: false,
    basis_at: "2026-11-25T12:00:00Z",
    ...over,
  };
}

function facets(items: HomeNeedItem[], key: "source" | "kind") {
  const counts = new Map<string, number>();
  for (const row of items) counts.set(row[key], (counts.get(row[key]) ?? 0) + 1);
  return [...counts].map(([value, count]) => ({ value, label: value, count }));
}

function needs(items: HomeNeedItem[], over: Partial<HomeNeeds> = {}): HomeNeeds {
  return {
    status: "ok",
    total: items.length,
    items,
    older_total: over.older_items?.length ?? 0,
    older_items: [],
    older_after_days: 90,
    sources: facets(items, "source"),
    kinds: facets(items, "kind"),
    people: [],
    parts: [],
    as_of: "2026-11-25T14:00:00Z",
    ...over,
  };
}

function rowTexts() {
  return screen.queryAllByTestId("need-row").map((row) => row.textContent ?? "");
}

beforeEach(() => {
  nav.search = "";
  nav.push.mockReset();
  nav.replace.mockReset();
  api.needs.mockReset();
  api.decide.mockReset();
  api.close.mockReset();
  publishNeedsCount(null, true);
  window.history.replaceState(null, "", "/");
});

describe("Sidebar navigation and Home tabs remain intact", () => {
  it("keeps the full owner sidebar menu intact and switches Home section tabs via ?show=", async () => {
    const secs = navSectionsFor({ owner: true, ops_reader: true, feedback_admin: true });
    expect(secs.filter((s) => s.group !== "account").map((s) => s.label)).toEqual([
      "Home",
      "Assist",
      "Ask",
      "Insights",
      "Data",
      "Meetings",
      "Team",
      "Admin",
    ]);

    api.needs.mockResolvedValue(needs([item(1)]));
    nav.search = "show=alerts";
    const alertsView = render(<HomePage />);
    await screen.findAllByRole("link", { name: "Alerts" });
    const tabs = screen.getByTestId("section-tabs-home");
    expect(within(tabs).getAllByRole("link").map((l) => [l.textContent, l.getAttribute("href")])).toEqual([
      ["Needs you", "/"],
      ["Alerts", "/?show=alerts"],
      ["Late POs", "/?show=late"],
    ]);
    expect(screen.queryByTestId("needs-you")).toBeNull();
    alertsView.unmount();

    nav.search = "show=late";
    const lateView = render(<HomePage />);
    expect(await screen.findByTestId("late-header")).toBeInTheDocument();
    lateView.unmount();

    nav.search = "";
    render(<HomePage />);
    expect(await screen.findByTestId("needs-you")).toBeInTheDocument();
  });
});

describe("Unified Needs-you list and inline filters", () => {
  it("renders one ranked list with plain English action labels and clean inline filter chips", async () => {
    api.needs.mockResolvedValue(
      needs([
        item(1, {
          kind: "reply",
          source: "mail",
          source_label: "Mail",
          person: "Maya",
          title: "Re: packing slip",
          next_act: "new_message",
          action_label: "Reply",
          draft_ready: true,
          body: "Thanks, checking now.",
        }),
        item(2, {
          kind: "late_po",
          source: "pos",
          source_label: "POs",
          person: "Acme",
          title: "PO 9182",
          action_label: "Check",
          days_late: 8,
        }),
        item(3, {
          kind: "alert",
          source: "alerts",
          source_label: "Alerts",
          title: "Batch on hold",
          action_label: "Decide",
        }),
      ]),
    );

    render(<HomePage />);
    await waitFor(() => expect(rowTexts()).toHaveLength(3));
    expect(rowTexts()[0]).toContain("Re: packing slip");
    expect(rowTexts()[0]).toContain("Maya · Mail · Reply");
    expect(rowTexts()[0]).toContain("Draft ready");
    expect(rowTexts()[0]).not.toContain("new_message");
    expect(rowTexts()[1]).toContain("PO 9182");
    expect(rowTexts()[2]).toContain("Batch on hold");

    // No cluttered category tablist inside Needs you header.
    const header = screen.getByTestId("home-header");
    expect(within(header).queryByRole("tab")).toBeNull();

    // Filter cleanly with inline kind chip.
    const filters = screen.getByTestId("kind-filters");
    fireEvent.click(within(filters).getByRole("button", { name: /^Late PO/ }));
    await waitFor(() => expect(rowTexts()).toHaveLength(1));
    expect(rowTexts()[0]).toContain("PO 9182");

    // Clear filters restores every item.
    fireEvent.click(screen.getByTestId("clear-filters"));
    await waitFor(() => expect(rowTexts()).toHaveLength(3));
  });

  it("publishes sidebar needs count only when status is ok", async () => {
    const seen: (number | null)[] = [];
    const Probe = () => {
      seen.push(useNeedsCount());
      return null;
    };
    api.needs.mockResolvedValue(needs([item(1), item(2)], { status: "partial", why: "Mail could not be read." }));
    const first = render(
      <>
        <HomePage />
        <Probe />
      </>,
    );
    await screen.findByTestId("needs-state");
    expect(seen.every((n) => n === null)).toBe(true);
    first.unmount();

    api.needs.mockResolvedValue(needs([item(1), item(2)], { status: "ok" }));
    render(
      <>
        <HomePage />
        <Probe />
      </>,
    );
    await waitFor(() => expect(seen[seen.length - 1]).toBe(2));
  });

  it("never silently cuts long lists and exposes Show more and Show all", async () => {
    api.needs.mockResolvedValue(needs(Array.from({ length: 125 }, (_, i) => item(i + 1))));
    render(<HomePage />);
    await waitFor(() => expect(rowTexts()).toHaveLength(100));
    const paging = screen.getByTestId("needs-paging");
    expect(paging).toHaveTextContent("Showing 100 of 125");
    fireEvent.click(within(paging).getByRole("button", { name: "Show all" }));
    await waitFor(() => expect(rowTexts()).toHaveLength(125));
  });
});

describe("Right-hand detail panel opens on click and closes on Close button or Escape", () => {
  it("opens an item on click, closes smoothly with Escape, and closes with the Close button", async () => {
    api.needs.mockResolvedValue(
      needs([
        item(1, { title: "PO 90412", line: "Confirm supplier ship date.", object_id: "PurchaseOrder:demo:90412", object_type: "PurchaseOrder", href: "/po/90412" }),
        item(2, { title: "SO 77120", line: "Customer waiting on update.", object_id: "SalesOrder:demo:77120", object_type: "SalesOrder", href: "/order/77120" }),
      ]),
    );

    render(<HomePage />);
    await waitFor(() => expect(rowTexts()).toHaveLength(2));
    expect(screen.queryByTestId("need-detail")).toBeNull();

    // Open by clicking the first item.
    fireEvent.click(screen.getAllByTestId("need-row")[0]);
    const detail = await screen.findByTestId("need-detail");
    expect(detail).toHaveTextContent("PO 90412");
    expect(detail).toHaveTextContent("Confirm supplier ship date.");

    // Close by pressing Escape.
    fireEvent.keyDown(window, { key: "Escape" });
    await waitFor(() => expect(screen.queryByTestId("need-detail")).toBeNull());

    // Open the second item and close via the Close button.
    fireEvent.click(screen.getAllByTestId("need-row")[1]);
    const detail2 = await screen.findByTestId("need-detail");
    expect(detail2).toHaveTextContent("SO 77120");
    fireEvent.click(screen.getByTestId("close-detail"));
    await waitFor(() => expect(screen.queryByTestId("need-detail")).toBeNull());
  });
});

describe("90-day fold keeps older items reachable in one click", () => {
  it("folds items older than 90 days into a collapsed section and expands them on click", async () => {
    api.needs.mockResolvedValue(
      needs(
        [item(1, { title: "Current PO 9100" })],
        {
          older_total: 2,
          older_after_days: 90,
          older_items: [
            item(2, { id: "late:old-1", kind: "late_po", source: "pos", source_label: "POs", title: "Old PO 8001", line: "Check old purchase order.", days_late: 120 }),
            item(3, { id: "late:old-2", kind: "late_po", source: "pos", source_label: "POs", title: "Old PO 8002", line: "Check old purchase order.", days_late: 145 }),
          ],
        },
      ),
    );

    render(<HomePage />);
    await waitFor(() => expect(rowTexts()).toHaveLength(1));
    expect(rowTexts()[0]).toContain("Current PO 9100");
    expect(screen.queryByText("Old PO 8001")).toBeNull();

    const toggle = screen.getByTestId("older-needs-toggle");
    expect(toggle).toHaveTextContent("Older than 90 days (2)");
    expect(toggle).toHaveAttribute("aria-expanded", "false");

    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    const olderList = screen.getByTestId("older-needs-list");
    expect(olderList).toHaveTextContent("Old PO 8001");
    expect(olderList).toHaveTextContent("Old PO 8002");

    // Clicking an older item opens it in the detail panel.
    fireEvent.click(within(olderList).getAllByTestId("need-row")[0]);
    const detail = await screen.findByTestId("need-detail");
    expect(detail).toHaveTextContent("Old PO 8001");
  });
});

describe("Gap alerts in Needs you (Genie item 9)", () => {
  it("shows undefined gap alerts for the owner and links to Data health", async () => {
    api.needs.mockResolvedValue(
      needs([
        item(1, {
          id: "gap:measure:fill_rate",
          kind: "gap",
          source: "gaps",
          source_label: "Gaps",
          title: "fill_rate (measure)",
          line: "Asked 12 times, no definition.",
          action: "decide",
          action_label: "Define",
          href: "/data-health#answers-gaps",
          object_id: null,
          object_type: null,
        }),
      ]),
    );

    render(<HomePage />);
    await waitFor(() => expect(rowTexts()).toHaveLength(1));
    expect(rowTexts()[0]).toContain("fill_rate (measure)");
    expect(rowTexts()[0]).toContain("Asked 12 times, no definition.");

    fireEvent.click(screen.getByTestId("need-row"));
    const detail = await screen.findByTestId("need-detail");
    const link = within(detail).getByRole("link", { name: "Open in Data health" });
    expect(link).toHaveAttribute("href", "/data-health#answers-gaps");
  });
});

describe("Error and empty states", () => {
  it("shows a calm retry notice when the list cannot be read", async () => {
    api.needs.mockRejectedValue(Object.assign(new Error("forced"), { code: "internal", message: "forced" }));
    render(<HomePage />);
    const list = await screen.findByTestId("needs-you");
    await waitFor(() => expect(list.hasAttribute("data-pending")).toBe(false));
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
  });

  it("shows one calm line when everything was read and nothing waits", async () => {
    api.needs.mockResolvedValue(needs([]));
    render(<HomePage />);
    expect(await screen.findByText("Nothing needs you right now.")).toBeInTheDocument();
  });

  it("formats Mission Control label, human dates, separate privacy badges, and alert next steps cleanly", async () => {
    api.needs.mockResolvedValue(
      needs([
        item(1, {
          id: "matter:mc1",
          kind: "task",
          source: "mc",
          source_label: "MC",
          title: "Mission Control task due 2026-10-29",
          line: "Mission Control task TASK-3601\nOnly you can see this.",
        }),
        item(2, {
          id: "alert:price1",
          kind: "alert",
          source: "alerts",
          source_label: "Alerts",
          title: "PHILLIP ADAM INC (NA)",
          line: "Price for Bottle 500ml dropped 28 % since 2026-06-24",
          ask: "Check the customer's agreed price for the product.",
        }),
      ]),
    );
    render(<HomePage />);
    await waitFor(() => expect(rowTexts()).toHaveLength(2));
    expect(rowTexts()[0]).toContain("Mission Control");
    expect(rowTexts()[0]).not.toMatch(/· MC ·/);
    expect(rowTexts()[0]).toContain("29 Oct");
    expect(rowTexts()[0]).not.toContain("2026-10-29");
    expect(screen.getByTestId("need-scope")).toHaveTextContent("Only you can see this");
    expect(rowTexts()[1]).toContain("28%");
    expect(rowTexts()[1]).not.toContain("28 %");
    expect(rowTexts()[1]).toContain("24 Jun");
    fireEvent.click(screen.getAllByTestId("need-row")[1]);
    const detail = await screen.findByTestId("need-detail");
    expect(within(detail).getByTestId("need-next-step")).toHaveTextContent("Check the customer's agreed price for the product.");
  });
});
