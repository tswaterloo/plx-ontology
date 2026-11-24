import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

// every read My work makes fails the way a server failure does
const { fail, ok, asked } = vi.hoisted(() => ({
  fail: () => Promise.reject(Object.assign(new Error("forced"), { code: "internal", message: "forced" })),
  // set by a test that reads successfully: what each read answers
  ok: { on: false, briefing: null as unknown, home: null as unknown, replies: null as unknown, drafts: [] as unknown[], briefingPending: false, suggestionsPending: false },
  // how often a group's page and the seen mark were asked for
  asked: { items: 0, seen: 0 },
}));
const nav = vi.hoisted(() => ({ search: "" }));
vi.mock("next/navigation", () => ({ useRouter: () => ({ push: vi.fn(), replace: vi.fn() }), useSearchParams: () => new URLSearchParams(nav.search), usePathname: () => "/" }));
vi.mock("@/lib/api", async (orig) => ({
  ...(await orig<typeof import("@/lib/api")>()),
  getBriefing: () => (ok.briefingPending ? new Promise(() => undefined) : ok.on ? Promise.resolve(ok.briefing) : fail()), getBriefingItems: () => (asked.items++, fail()), getBriefingPages: () => (asked.items++, fail()), getSuggestions: () => (ok.suggestionsPending ? new Promise(() => undefined) : fail()), getActionStates: fail, markBriefingSeen: () => (asked.seen++, fail()),
  getReplies: () => (ok.on && ok.replies ? Promise.resolve(ok.replies) : fail()),
  getAutopilotDrafts: () => (ok.on ? Promise.resolve({ items: ok.drafts, never_senders: [] }) : fail()),
  getAutopilot: fail,
}));
vi.mock("@/components/shell", async (orig) => ({ ...(await orig<typeof import("@/components/shell")>()), loadHome: () => (ok.on ? Promise.resolve(ok.home) : fail()), loadLateRead: () => (ok.on ? Promise.resolve({ items: [], total: 0 }) : fail()) }));
vi.mock("@/components/needs-me", () => ({ NeedsMe: () => null }));

import HomePage from "./page";
import { publishNeedsCount, useNeedsCount } from "@/components/shell";

describe("Home's three tabs (simpler tabs, 11-18): what needs you, Alerts (was /watching), Late POs (was /late)", () => {
  const tabs = () => screen.getByTestId("section-tabs-home");
  const on = () => within(tabs()).getAllByRole("link").filter((a) => a.getAttribute("aria-current") === "page").map((a) => a.textContent);
  it("?show= picks the tab; an unknown one opens what needs you", async () => {
    try {
      nav.search = "show=alerts";
      const a = render(<HomePage />);
      // the Alerts and Late POs tabs load as their own chunks (first drawn as "Reading ...")
      await screen.findAllByRole("link", { name: "Alerts" });
      expect(within(tabs()).getAllByRole("link").map((l) => [l.textContent, l.getAttribute("href")])).toEqual([["Needs you", "/"], ["Alerts", "/?show=alerts"], ["Late POs", "/?show=late"], ["Switches", "/autopilot"]]);
      expect(on()).toEqual(["Alerts"]);
      expect(screen.queryByTestId("needs-you")).toBeNull();
      a.unmount();
      nav.search = "show=late&supplier=Acme";
      const l = render(<HomePage />);
      expect(await screen.findByTestId("late-header")).toBeInTheDocument();
      expect(on()).toEqual(["Late POs"]);
      l.unmount();
      nav.search = "show=../../ops";
      render(<HomePage />);
      expect(await screen.findByTestId("needs-you")).toBeInTheDocument();
      expect(on()).toEqual(["Needs you"]);
    } finally {
      nav.search = "";
    }
  });
});

describe("My work when nothing can be read", () => {
  it("shows the failure once and is no longer marked as still loading", async () => {
    render(<HomePage />);
    const list = await screen.findByTestId("needs-you");
    await waitFor(() => expect(list).toHaveTextContent("Couldn't load your work."));
    await waitFor(() => expect(list.hasAttribute("data-pending")).toBe(false));
    expect(screen.getAllByRole("button", { name: "Try again" })).toHaveLength(1);
  });
});

describe("Home never waits on the suggestion cards", () => {
  it("lists the late orders and stops being marked loading while the cards are still read", async () => {
    ok.on = true;
    ok.suggestionsPending = true;
    ok.home = { needs_attention: { status: "read", late_total: 0, late_pos: [] } };
    ok.briefing = { status: "read", count: 0, items: [], groups: [] };
    ok.replies = { total: 0, items: [] };
    try {
      render(<HomePage />);
      await screen.findByText("Nothing needs you right now.");
      expect(screen.getByTestId("needs-you").hasAttribute("data-pending")).toBe(false);
    } finally {
      ok.on = false;
      ok.suggestionsPending = false;
    }
  });
});

describe("Home keeps the checks' gaps on the alert pages", () => {
  it("never lists what a check could not judge", async () => {
    ok.on = true;
    ok.home = { needs_attention: { status: "read", late_total: 0, late_pos: [] } };
    ok.briefing = {
      status: "read", count: 0, items: [], groups: [],
      data_note_items: [{ check: "late_so", reason: "no_lines", words: "late customer orders: cannot check 12 records that lack order lines", count: 12 }],
    };
    ok.replies = { total: 0, items: [] };
    render(<HomePage />);
    // one sentence, only once every part (late, alerts, replies, prepared) was read as empty
    await screen.findByText("Nothing needs you right now.");
    expect(screen.queryByText(/cannot check/)).toBeNull();
    expect(screen.queryByTestId("briefing-data-notes")).toBeNull();
    ok.on = false;
  });

  it("publishes one count for the sidebar only when every part was read; a failed part leaves it unknown, never 0", async () => {
    ok.on = true;
    ok.home = { needs_attention: { status: "read", late_total: 0, late_pos: [] } };
    ok.briefing = { status: "read", count: 0, items: [], groups: [] };
    ok.replies = null; // the inbox read fails
    publishNeedsCount(null);
    const seen: (number | null)[] = [];
    const Probe = () => {
      seen.push(useNeedsCount());
      return null;
    };
    render(<><HomePage /><Probe /></>);
    await waitFor(() => expect(screen.getByTestId("needs-you").hasAttribute("data-pending")).toBe(false));
    await new Promise((r) => setTimeout(r, 30));
    expect(screen.queryByText("Nothing needs you right now.")).toBeNull();
    expect(seen.every((n) => n === null)).toBe(true);
    ok.on = false;
  });

  it("says no 'nothing needs action' above replies it lists while the checks re-run", async () => {
    ok.on = true;
    ok.home = { needs_attention: { status: "read", late_total: 0, late_pos: [], refreshing: true, checked_on: "2026-11-17T19:13:00Z" } };
    ok.briefing = { status: "read", count: 0, items: [], groups: [], refreshing: true, checked_on: "2026-11-17T19:13:00Z" };
    ok.replies = { total: 2, items: [] };
    render(<HomePage />);
    await screen.findByTestId("home-replies");
    expect(screen.queryByTestId("briefing-refreshing")).toBeNull();
    expect(screen.queryByText(/Nothing needs/)).toBeNull();
    ok.on = false;
  });
  it("on a desktop opens the first reply by itself, and an empty Home lets the list take the whole width", async () => {
    const offers = ["send_email", "teams_message", "mark_done", "snooze", "dismiss"].map((kind) => ({ kind, label: kind, outbound: false }));
    const was = window.matchMedia;
    window.matchMedia = (q: string) => ({ matches: true, media: q, addEventListener: () => {}, removeEventListener: () => {} }) as unknown as MediaQueryList;
    ok.on = true;
    ok.home = { needs_attention: { status: "read", late_total: 0, late_pos: [] } };
    ok.briefing = { status: "read", count: 0, items: [], groups: [] };
    ok.replies = { total: 1, as_of: "2026-11-17", sent: 0, writes: 0, lines: [{ id: "r1", kind: "reply_owed", channel: "mail", days: 3, chases: 0, subject: "Where is my order", draft: { channel: "email", to: [], subject: "Re", body: "stub", status: "draft_not_sent", sent: false }, rank: [1], new_inquiry: false, card_id: "c1", offers }] };
    const { unmount } = render(<HomePage />);
    const detail = await screen.findByTestId("reply-detail");
    expect(detail).toHaveTextContent("Where is my order");
    expect(detail.querySelector("[data-testid=action-send_email]")).toHaveTextContent("Reply");
    unmount();
    ok.replies = { total: 0, as_of: "2026-11-17", sent: 0, writes: 0, lines: [] };
    render(<HomePage />);
    await screen.findByText("Nothing needs you right now.");
    expect(screen.queryByTestId("reply-detail")).toBeNull();
    expect(screen.getByTestId("briefing").className).toContain("lg:max-w-none");
    expect(screen.getByTestId("briefing").className).not.toContain("lg:w-[368px]");
    window.matchMedia = was;
    ok.on = false;
  });
});

describe("Home reads a group's further items only when it can show them", () => {
  it("reads no group page when every item sent home is among the top ones, and still marks the visit seen", async () => {
    ok.on = true;
    ok.home = { needs_attention: { status: "read", late_total: 0, late_pos: [] } };
    // 65 of a kind that waits in Watching (none sent home): Home shows none of them
    ok.briefing = {
      status: "read", count: 65, home_count: 0, items: [],
      groups: [{ event_type: "price_below_cost", label: "Below cost", count: 65, in_top: 0, new_since_visit: 0, acknowledged: 0 }],
    };
    ok.replies = { total: 0, items: [] };
    asked.items = 0;
    asked.seen = 0;
    render(<HomePage />);
    await waitFor(() => expect(asked.seen).toBe(1));
    expect(asked.items).toBe(0);
    ok.on = false;
  });

  it("takes the group's page from the briefing itself (no second request) and marks the visit seen at once", async () => {
    ok.on = true;
    ok.home = { needs_attention: { status: "read", late_total: 0, late_pos: [] } };
    const item = { id: "r1", event_type: "reply_needed", object_type: "Batch", object_id: "Batch:1", subject_label: "BC-1", summary: "Reply is due to BC-1.", home: true, rank: 1 };
    ok.briefing = {
      status: "read", count: 9, home_count: 2, items: [],
      groups: [{ event_type: "reply_needed", label: "Reply", count: 9, in_top: 0, new_since_visit: 0, acknowledged: 0 }],
      pages: [{ event_type: "reply_needed", total: 9, offset: 0, items: [item], more: true }],
    };
    ok.replies = { total: 0, items: [] };
    asked.items = 0;
    asked.seen = 0;
    render(<HomePage />);
    await waitFor(() => expect(asked.seen).toBe(1));
    expect(asked.items).toBe(0);
    expect(await screen.findByText("Reply is due to BC-1.", { exact: false })).toBeInTheDocument();
    ok.on = false;
  });
});

describe("Home stays a short list", () => {
  it("lists a few alerts of a kind and the rest one click away, the late orders all", async () => {
    ok.on = true;
    ok.home = { needs_attention: { status: "read", late_total: 0, late_pos: [] } };
    const item = (n: number) => ({ id: `a${n}`, event_type: "lot_on_hold", object_type: "Batch", object_id: `Batch:${n}`, subject_label: `BC-${n}`, summary: `Lot ${n} is on quality hold.`, home: true, rank: n });
    ok.briefing = {
      status: "read", count: 7, home_count: 7, items: [1, 2, 3, 4, 5, 6, 7].map(item),
      groups: [{ event_type: "lot_on_hold", label: "Batch on quality hold", count: 7, in_top: 7, new_since_visit: 0, acknowledged: 0 }],
    };
    ok.replies = { total: 0, items: [] };
    render(<HomePage />);
    const more = await screen.findByTestId("home-folded");
    expect(more).toHaveTextContent("Show 4 more");
    expect(screen.getByText("Lot 3 is on quality hold.", { exact: false })).toBeInTheDocument();
    expect(screen.queryByText("Lot 4 is on quality hold.", { exact: false })).toBeNull();
    fireEvent.click(more);
    expect(await screen.findByText("Lot 7 is on quality hold.", { exact: false })).toBeInTheDocument();
    ok.on = false;
  });
});

describe("Home counts an alert kind the way Alerts does", () => {
  it("heads a group with every alert of its kind firing, not the rows read so far", async () => {
    ok.on = true;
    ok.home = { needs_attention: { status: "read", late_total: 0, late_pos: [] } };
    const item = (n: number) => ({ id: `c${n}`, event_type: "price_below_cost", object_type: "Customer", object_id: `Customer:${n}`, subject_label: `C-${n}`, summary: `Customer ${n} paid below cost.`, home: true, rank: n });
    // 65 fire; the briefing's first page holds 25 of them
    ok.briefing = {
      status: "read", count: 65, home_count: 65, items: Array.from({ length: 25 }, (_, i) => item(i + 1)),
      groups: [{ event_type: "price_below_cost", label: "Customer price below cost", count: 65, in_top: 25, new_since_visit: 0, acknowledged: 0 }],
    };
    ok.replies = { total: 0, items: [] };
    render(<HomePage />);
    const more = await screen.findByTestId("home-folded");
    expect(more).toHaveTextContent("Show 62 more");
    expect(screen.getByRole("heading", { name: /below cost\s*65/i })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: /below cost\s*25/i })).toBeNull();
    ok.on = false;
  });
});

describe("Home gathers the registry's Financials into one compact section", () => {
  it("lists a count and the biggest few with their money, opens the full list, and keeps them out of what needs you", async () => {
    ok.on = true;
    ok.home = { needs_attention: { status: "read", late_total: 0, late_pos: [] } };
    const money = (n: number, id: string, basis: string, event: string) => ({ id, event_type: event, object_type: "Customer", object_id: `Customer:${id}`, subject_label: `Cust ${n}`, summary: `Cust ${n} paid 80% of cost.`, home: true, section: "financials", alert_kind: "margin_loss", rank: n, amount: { amount: String(6576.48 * n), currency: "CAD" }, amount_basis: basis });
    ok.briefing = {
      status: "read", count: 71, home_count: 4, watching_count: 0,
      items: [
        { id: "l1", event_type: "lot_on_hold", object_type: "Batch", object_id: "Batch:1", subject_label: "BC-1", summary: "Batch BC-1 is on quality hold.", home: true, rank: 1, alert_kind: "quality_hold" },
        ...[3, 2, 1].map((n) => money(n, `m${n}`, "below_cost", "price_below_cost")),
      ],
      groups: [
        { event_type: "lot_on_hold", label: "Batch on quality hold", count: 4, in_top: 1, new_since_visit: 0, acknowledged: 0 },
        { event_type: "price_below_cost", label: "Customer price below cost", count: 67, in_top: 3, new_since_visit: 0, acknowledged: 0, section: "financials" },
      ],
    };
    ok.replies = { total: 0, items: [] };
    render(<HomePage />);
    const box = await screen.findByTestId("home-financials");
    expect(within(box).getByRole("heading", { name: /Financials\s*67/ })).toBeInTheDocument();
    // the biggest few in the order given, each with its money and thousands separators
    const rows = within(box).getAllByTestId("briefing-item");
    expect(rows).toHaveLength(3);
    expect(rows[0]).toHaveTextContent("$19,729.44 CAD");
    // the full list is one click away and says how many it holds
    expect(within(box).getByRole("link", { name: "All 67" }).getAttribute("href")).toBe("/?show=alerts#kind-margin_loss");
    // needs you: the batches only (4); the 67 are not 67 things to act on
    expect(screen.getByRole("heading", { name: /Quality holds\s*4/ })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: /below cost/i })).toBeNull();
    expect(screen.getAllByTestId("briefing-item")).toHaveLength(4);
    ok.on = false;
  });
});

describe("One section for holds", () => {
  it("lists a prepared hold card in its kind's section, first, and nowhere else", async () => {
    ok.on = true;
    ok.home = { needs_attention: { status: "read", late_total: 0, late_pos: [] } };
    ok.briefing = {
      status: "read", count: 1, home_count: 1,
      items: [{ id: "l1", event_type: "lot_on_hold", object_type: "Batch", object_id: "Batch:1", subject_label: "BC-1", summary: "Batch BC-1 is on quality hold.", home: true, rank: 1, alert_kind: "quality_hold" }],
      groups: [{ event_type: "lot_on_hold", label: "Batch on quality hold", count: 1, in_top: 1, new_since_visit: 0, acknowledged: 0 }],
    };
    ok.replies = { total: 0, items: [] };
    ok.drafts = [{ item_id: "q1", sender: "", channel: "system", subject: "QC hold or fail", message: "", kind: "alert", alert_kind: "quality_hold", to: [], cc: [], body: "", edited: false, summary: "Lot BC-9 is on-hold; 1 open order(s) affected.", ask: "Lot BC-9 is on-hold; 1 open order(s) affected.", facts: [], created_at: "2026-11-21T12:00:00Z" }];
    render(<HomePage />);
    await screen.findByRole("heading", { name: /Quality holds/ });
   
    const card = await screen.findAllByText(/Lot BC-9 is on-hold; 1 open order affected/);
    expect(card).toHaveLength(1);
    const section = screen.getByRole("heading", { name: /Quality holds/ }).parentElement!.parentElement!;
    expect(section).toHaveTextContent("Holding up an order");
    expect(section).toHaveTextContent("Lot BC-9");
    expect(screen.queryByTestId("assist-queue")).toBeNull();
    ok.drafts = [];
    ok.on = false;
  });
});

describe("The visit is marked seen after the suggestion cards have settled", () => {
  it("sends no mark while the cards are still being read (the mark makes their kept read stale)", async () => {
    ok.on = true;
    ok.suggestionsPending = true;
    ok.home = { needs_attention: { status: "read", late_total: 0, late_pos: [] } };
    ok.briefing = { status: "read", count: 0, home_count: 0, items: [], groups: [] };
    ok.replies = { total: 0, items: [] };
    asked.seen = 0;
    const { unmount } = render(<HomePage />);
    await screen.findAllByRole("heading", { name: "Home" });
    await new Promise((r) => setTimeout(r, 400));
    expect(asked.seen).toBe(0);
    // leaving before the cards settle still marks the visit seen (once)
    unmount();
    expect(asked.seen).toBe(1);
    ok.suggestionsPending = false;
  });
});

describe("Home reads no page it would fold away", () => {
  it("reads no group page when the top items already fill the rows Home shows of that kind", async () => {
    ok.on = true;
    ok.home = { needs_attention: { status: "read", late_total: 0, late_pos: [] } };
    const item = (n: number) => ({ id: `b${n}`, event_type: "lot_on_hold", object_type: "Batch", object_id: `Batch:${n}`, subject_label: `BC-${n}`, summary: `Lot ${n} is on quality hold.`, home: true, rank: n });
    ok.briefing = {
      status: "read", count: 18, home_count: 18, items: [1, 2, 3, 4].map(item),
      groups: [{ event_type: "lot_on_hold", label: "Batch on quality hold", count: 18, in_top: 4, new_since_visit: 0, acknowledged: 0 }],
    };
    ok.replies = { total: 0, items: [] };
    asked.items = 0;
    asked.seen = 0;
    render(<HomePage />);
    await waitFor(() => expect(asked.seen).toBe(1));
    expect(asked.items).toBe(0);
    ok.on = false;
  });
});

describe("Home while the alerts are still being read", () => {
  it("shows the late orders and replies at once and one line for the alerts, never a page of skeleton rows", async () => {
    ok.on = true;
    ok.briefingPending = true;
    ok.home = { needs_attention: { status: "read", late_total: 1, late_pos: [{ object_id: "PurchaseOrder:demo:90388", label: "90388", supplier: "Northwind", days_late: 12, due_date: "2026-11-01", open_url: "/po/90388" }] } };
    ok.replies = { total: 0, items: [] };
    render(<HomePage />);
    expect(await screen.findByText("90388", { exact: false })).toBeInTheDocument();
    expect(screen.getByTestId("alerts-reading")).toHaveTextContent("Reading your alerts");
    expect(screen.queryByLabelText("Loading your work")).toBeNull();
    ok.briefingPending = false;
    ok.on = false;
  });

  it("says the alerts are being read when nothing else is there yet", async () => {
    ok.on = true;
    ok.briefingPending = true;
    ok.home = { needs_attention: { status: "read", late_total: 0, late_pos: [] } };
    ok.replies = { total: 0, items: [] };
    render(<HomePage />);
    expect(await screen.findByTestId("alerts-reading")).toBeInTheDocument();
    expect(screen.queryByLabelText("Loading your work")).toBeNull();
    expect(screen.queryByText("Nothing needs you right now.")).toBeNull();
    ok.briefingPending = false;
    ok.on = false;
  });
});

describe("Home's counts add up", () => {
  it("All is the sum of its chips: a draft to confirm counts under Confirm, an alert waiting on you (nothing to confirm) under Decide", async () => {
    ok.on = true;
    ok.home = { needs_attention: { status: "read", late_total: 0, late_pos: [] } };
    const item = (n: number) => ({ id: `h${n}`, event_type: "lot_on_hold", object_type: "Batch", object_id: `Batch:${n}`, subject_label: `BC-${n}`, summary: `Lot ${n} is on quality hold.`, home: true, rank: n });
    ok.briefing = {
      status: "read", count: 3, home_count: 3, items: [1, 2, 3].map(item),
      groups: [{ event_type: "lot_on_hold", label: "Batch on quality hold", count: 3, in_top: 3, new_since_visit: 0, acknowledged: 0 }],
    };
    ok.replies = { total: 0, items: [] };
    const alert = { item_id: "ap_q", sender: "", channel: "system", kind: "alert", subject: "QC hold or fail", message: "", body: "", to: [], cc: [], edited: false, ask: "", summary: "Lot L-1 is on-hold; it holds up 1 open order.", facts: ["OR-9, Three Ships"], note: "", links: [], created_at: "2026-11-18T14:00:00Z" };
    ok.drafts = [alert, { ...alert, item_id: "ap_r", summary: "Lot L-2 is on-hold; it holds up 1 open order." }];
    try {
      const { unmount } = render(<HomePage />);
      const header = await screen.findByTestId("home-header");
      // one kind of thing waiting (Decide 5 is All 5): no chips, the list says it
      await waitFor(() => expect(screen.getByTestId("assist-queue")).toHaveAccessibleName("Waiting on you"));
      expect(within(header).queryByRole("tab")).toBeNull();
      expect(screen.queryByText("Prepared for you")).toBeNull();
      unmount();
      // a draft to confirm is prepared for you and counts under Confirm alone
      ok.drafts = [alert, { ...alert, item_id: "ap_d", kind: "reply", subject: "Re: order", summary: "Reply drafted." }];
      render(<HomePage />);
      const header2 = await screen.findByTestId("home-header");
      const chip2 = (name: string) => within(header2).getByRole("tab", { name: new RegExp(`^${name}`) });
      await waitFor(() => expect(chip2("All")).toHaveTextContent("All5"));
      expect(chip2("Confirm")).toHaveTextContent("Confirm1");
      expect(chip2("Decide")).toHaveTextContent("Decide4");
      fireEvent.click(chip2("Confirm"));
      expect(screen.getByTestId("assist-queue")).toHaveAccessibleName("Prepared for you");
      expect(screen.queryByText("Lot 1 is on quality hold.", { exact: false })).toBeNull();
    } finally {
      ok.on = false;
      ok.drafts = [];
    }
  });
});
