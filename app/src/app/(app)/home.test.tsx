import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
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
  forgetKeptReads();
  nav.search = "";
  nav.push.mockReset();
  nav.replace.mockReset();
  api.needs.mockReset();
  api.decide.mockReset();
  api.close.mockReset();
  publishNeedsCount(null, true);
  window.history.replaceState(null, "", "/");
});

describe("Sidebar navigation and Home deep links remain intact", () => {
  it("folds Meetings into Home, removes redundant Home SectionTabs, and keeps ?show=alerts and ?show=late working", async () => {
    const secs = navSectionsFor({ owner: true, ops_reader: true, feedback_admin: true });
    expect(secs.filter((s) => s.group !== "account").map((s) => s.label)).toEqual([
      "Home",
      "Assist",
      "Ask",
      "Insights",
      "Artifacts",
      "Data",
      "Team",
      "Admin",
    ]);

    api.needs.mockResolvedValue(needs([item(1)]));
    nav.search = "show=alerts";
    const alertsView = render(<HomePage />);
    await screen.findByRole("heading", { name: "What fired" });
    expect(screen.queryByTestId("section-tabs-home")).toBeNull();
    expect(screen.queryByTestId("needs-you")).toBeNull();
    alertsView.unmount();

    nav.search = "show=late";
    const lateView = render(<HomePage />);
    expect(await screen.findByTestId("late-header")).toBeInTheDocument();
    expect(screen.queryByTestId("section-tabs-home")).toBeNull();
    lateView.unmount();

    nav.search = "";
    render(<HomePage />);
    expect(await screen.findByTestId("needs-you")).toBeInTheDocument();
    expect(screen.queryByTestId("section-tabs-home")).toBeNull();
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
    expect(rowTexts()[0]).toContain("Maya");
    expect(rowTexts()[0]).toContain("Mail");
    expect(rowTexts()[0]).toContain("Draft ready");
    expect(rowTexts()[0]).not.toContain("new_message");
    expect(rowTexts()[1]).toContain("PO 9182");
    expect(rowTexts()[2]).toContain("Batch on hold");

    // No cluttered category tablist inside Home header.
    const header = screen.getByTestId("home-header");
    expect(within(header).queryByRole("tab")).toBeNull();

    // Filter cleanly with unified section pill ("Alerts & late" holds both late PO and alert).
    const filters = screen.getByTestId("section-filters");
    fireEvent.click(within(filters).getByTestId("filter-section-holds_alerts"));
    await waitFor(() => expect(rowTexts()).toHaveLength(2));
    expect(rowTexts()[0]).toContain("PO 9182");
    expect(rowTexts()[1]).toContain("Batch on hold");

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
    await waitFor(() => expect(rowTexts()).toHaveLength(7));
    const paging = screen.getByTestId("needs-paging");
    expect(paging).toHaveTextContent("Showing 7 of 125");
    fireEvent.click(within(paging).getByRole("button", { name: "Show all" }));
    await waitFor(() => expect(rowTexts()).toHaveLength(125));
  });
});

describe("Inline detail expansion on Home rows and canonical record links", () => {
  it("opens clean without an empty split pane, expands a row inline on click, normalizes 1-segment object hrefs, and closes on Escape or Close button", async () => {
    api.needs.mockResolvedValue(
      needs([
        item(1, {
          title: "PO 90412",
          line: "Confirm supplier ship date.",
          object_id: "PurchaseOrder:demo:90412",
          object_type: "PurchaseOrder",
          href: "/object/PurchaseOrder:demo:90412",
        }),
        item(2, {
          title: "SO 77120",
          line: "Customer waiting on update.",
          object_id: "SalesOrder:demo:77120",
          object_type: "SalesOrder",
          href: "/order/77120",
        }),
      ]),
    );

    render(<HomePage />);
    await waitFor(() => expect(rowTexts()).toHaveLength(2));
    // Starts clean as a full-width list with no pre-opened detail pane.
    expect(screen.queryByTestId("need-detail")).toBeNull();

    // Click first row to expand inline detail with canonical 2-segment /object/<Type>/<id> link.
    fireEvent.click(screen.getAllByTestId("need-row")[0]);
    const detail = await screen.findByTestId("need-detail");
    expect(detail).toHaveTextContent("PO 90412");
    expect(detail).toHaveTextContent("Confirm supplier ship date.");
    expect(within(detail).getByRole("link", { name: /Open the full record/ })).toHaveAttribute(
      "href",
      "/object/PurchaseOrder/PurchaseOrder%3Ademo%3A90412",
    );

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

  it("formats Mission Control label, human dates, quiet private lock indicator, ops grouping, and alert next steps cleanly", async () => {
    api.needs.mockResolvedValue(
      needs([
        item(1, {
          id: "matter:mc1",
          kind: "task",
          source: "mc",
          source_label: "MC",
          title: "Mission Control task due 2026-10-29",
          line: "Still open? Nothing has happened on it since it was due 29 Oct. Mission Control task TASK-3601\nOnly you can see this.",
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
        item(3, {
          id: "ops:1",
          kind: "reply",
          source: "mail",
          source_label: "Mail",
          title: "Workbench API health 14 Nov: ALERT",
          line: "Investigate the 8 unavailable Workbench API relations per the runbook.",
        }),
        item(4, {
          id: "ops:2",
          kind: "reply",
          source: "mail",
          source_label: "Mail",
          title: "Workbench API health 15 Nov: ALERT",
          line: "Investigate the 8 unavailable Workbench API relations per the runbook.",
        }),
      ]),
    );
    render(<HomePage />);
    await waitFor(() => expect(rowTexts()).toHaveLength(2));
    expect(rowTexts()[0]).toContain("28%");
    expect(rowTexts()[0]).not.toContain("28 %");
    expect(rowTexts()[0]).toContain("24 Jun");
    expect(rowTexts()[1]).toContain("Mission Control");
    expect(rowTexts()[1]).not.toMatch(/· MC ·/);
    expect(rowTexts()[1]).toContain("29 Oct");
    expect(rowTexts()[1]).not.toContain("2026-10-29");
    expect(rowTexts()[1]).not.toContain("Still open? Nothing has happened");
    expect(screen.getByTestId("need-private")).toHaveAttribute("aria-label", "Only you can see this");

    // Repetitive internal ops alerts are collapsed into System and ops notices.
    const opsToggle = screen.getByTestId("ops-notices-toggle");
    expect(opsToggle).toHaveTextContent("System and ops notices (2)");
    fireEvent.click(opsToggle);
    expect(screen.getByTestId("ops-notices-list")).toHaveTextContent("Workbench API health 14 Nov: ALERT");

    fireEvent.click(screen.getAllByTestId("need-row")[0]);
    const detail = await screen.findByTestId("need-detail");
    expect(within(detail).getByTestId("need-next-step")).toHaveTextContent("Check the customer's agreed price for the product.");
  });
});

describe("Compact Financials block and consolidated Holds group (PLAN-P2-C6)", () => {
  it("folds price_drop and below_cost alerts into one compact expandable Financials block without dropping any row", async () => {
    const priceDrops = Array.from({ length: 24 }, (_, i) =>
      item(100 + i, {
        id: `alert:pd:${i + 1}`,
        kind: "alert",
        source: "alerts",
        source_label: "Alerts",
        title: `Customer Drop ${i + 1}`,
        line: `Customer Drop ${i + 1}'s price dropped 18% (CAD per EA) against the 180 days before.`,
        event_type: "customer_price_drop",
        alert_kind: "price_exception",
        section: "financials",
        badge: "Price drop",
        action: "decide",
        action_label: "Decide",
      }),
    );
    const belowCosts = Array.from({ length: 67 }, (_, i) =>
      item(200 + i, {
        id: `alert:bc:${i + 1}`,
        kind: "alert",
        source: "alerts",
        source_label: "Alerts",
        title: `Customer Cost ${i + 1}`,
        line:
          i === 0
            ? "Customer Cost 1 paid 23% of cost for Serum 30ml (CAD per EA) over the last 180 days."
            : `Customer Cost ${i + 1} paid 82% of cost for Bottle 500ml (CAD per EA) over the last 180 days.`,
        ask: i === 0 ? "Booked cost is over 2x revenue; verify unit cost before treating as margin loss." : undefined,
        event_type: "price_below_cost",
        alert_kind: "margin_loss",
        section: "financials",
        badge: i === 0 ? "Verify unit cost" : "Below cost",
        action: "decide",
        action_label: "Decide",
      }),
    );
    const regular = [
      item(1, { kind: "reply", source: "mail", source_label: "Mail", title: "Urgent supplier reply", action: "reply", action_label: "Reply" }),
      item(2, { kind: "late_po", source: "pos", source_label: "POs", title: "PO 90412", action: "check", action_label: "Check", days_late: 5 }),
    ];

    api.needs.mockResolvedValue(needs([...regular, ...priceDrops, ...belowCosts]));
    render(<HomePage />);

    // Regular items lead the queue; the 91 financial alerts fold into one compact summary row.
    await waitFor(() => expect(rowTexts()).toHaveLength(2));
    expect(rowTexts()[0]).toContain("Urgent supplier reply");
    expect(rowTexts()[1]).toContain("PO 90412");

    const toggle = screen.getByTestId("financials-toggle");
    expect(toggle).toHaveTextContent("Financials · 24 price drops, 67 below cost");
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByTestId("financials-list")).toBeNull();

    // Expanding exposes the calm top-7 slice with an explicit Show all revealing all 91 alerts (zero silent drops).
    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    const finList = screen.getByTestId("financials-list");
    expect(within(finList).getAllByTestId("need-row")).toHaveLength(7);
    const finSection = finList.parentElement!;
    fireEvent.click(within(finSection).getByRole("button", { name: "Show all" }));
    const finRows = within(finList).getAllByTestId("need-row");
    expect(finRows).toHaveLength(91);
    expect(finRows[24]).toHaveTextContent("Verify unit cost");

    // Clicking a row inside Financials opens its detail panel cleanly.
    fireEvent.click(finRows[24]);
    const detail = await screen.findByTestId("need-detail");
    expect(detail).toHaveTextContent("Customer Cost 1");
    expect(within(detail).getByTestId("need-detail-badge")).toHaveTextContent("Verify unit cost");
    expect(detail.textContent).not.toContain("\u2014");
  });

  it("merges QC holds, batch holds, and order holds into one Holds group while preserving each item's specific badge", async () => {
    api.needs.mockResolvedValue(
      needs([
        item(1, { kind: "late_po", source: "pos", source_label: "POs", title: "PO 90412", action: "check", action_label: "Check" }),
        item(2, {
          id: "matter:qc1",
          kind: "alert",
          source: "alerts",
          source_label: "Alerts",
          title: "QC hold: Lot L-100",
          line: "Lot L-100 is on-hold; 1 open order affected.",
          event_type: "qc_hold",
          alert_kind: "quality_hold",
          section: "holds",
          badge: "QC hold",
          action: "decide",
          action_label: "Decide",
        }),
        item(3, {
          id: "alert:lot1",
          kind: "alert",
          source: "alerts",
          source_label: "Alerts",
          title: "Batch B-204",
          line: "Batch B-204 is on quality hold since it was made on 14 Nov.",
          event_type: "lot_on_hold",
          alert_kind: "quality_hold",
          section: "holds",
          badge: "Batch hold",
          action: "decide",
          action_label: "Decide",
        }),
        item(4, {
          id: "alert:ord1",
          kind: "alert",
          source: "alerts",
          source_label: "Alerts",
          title: "SO 77120",
          line: "Order SO 77120 is on hold.",
          event_type: "order_hold",
          alert_kind: "quality_hold",
          section: "holds",
          badge: "Order hold",
          action: "decide",
          action_label: "Decide",
        }),
      ]),
    );

    render(<HomePage />);
    const holdsGroup = await screen.findByTestId("holds-group");
    const holdsToggle = within(holdsGroup).getByTestId("holds-toggle");
    expect(holdsToggle).toHaveTextContent("Holds (3)");
    expect(holdsToggle).toHaveAttribute("aria-expanded", "true");

    const holdsList = within(holdsGroup).getByTestId("holds-list");
    const badges = within(holdsList)
      .getAllByTestId("need-badge")
      .map((el) => el.textContent);
    expect(badges).toEqual(["QC hold", "Batch hold", "Order hold"]);

    // Collapsing and expanding Holds works cleanly.
    fireEvent.click(holdsToggle);
    expect(within(holdsGroup).queryByTestId("holds-list")).toBeNull();
    fireEvent.click(holdsToggle);
    expect(within(holdsGroup).getAllByTestId("need-row")).toHaveLength(3);
  });
});
