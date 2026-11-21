import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ApiError } from "@/lib/errors";
import { describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({ briefing: null as null | (() => Promise<unknown>), watchlist: { configured: true, alerts: [], follows: [], rules: [], unread: 0 } as Record<string, unknown> }));
vi.mock("@/lib/api", async (orig) => ({
  ...(await orig<typeof import("@/lib/api")>()),
  getWatchlist: () => Promise.resolve(api.watchlist),
  getAlertKinds: () =>
    Promise.resolve({
      rule: "",
      kinds: [{ kind: "late_po", label: "Late PO", surface: "alert" }, { kind: "hold", label: "Lot on quality hold", surface: "alert" }],
      types: [{ type: "po_late", kind: "late_po", label: "Late purchase order", default: "on", home: true, precision: null }],
    }),
  getBriefing: () => api.briefing!(),
}));

import { AlertsView as WatchingPage } from "./alerts-view";

const figure = (label: string) => screen.getByText(label).nextElementSibling?.textContent;

describe("Watching figures", () => {
  it("claims no count until what fired has been read", async () => {
    api.briefing = () => new Promise(() => {});
    render(<WatchingPage />);
    await screen.findByText("Late PO");
    expect(figure("Firing now")).toBe("·");
  });

  it("counts what fires once read, and no count of kinds (a number nobody acts on)", async () => {
    api.briefing = () => Promise.resolve({ status: "read", count: 2, new_since_visit: 0, groups: [{ event_type: "po_late", count: 2, in_top: 0, new_since_visit: 0, label: "Late purchase order" }], items: [] });
    render(<WatchingPage />);
    await waitFor(() => expect(figure("Firing now")).toBe("2"));
    expect(screen.queryByText("Kinds firing")).toBeNull();
    // nothing new, no rules, no follows: those counters are not drawn at all
    const figs = within(screen.getByTestId("watching-figures"));
    expect(figs.queryByText("New since your visit")).toBeNull();
    expect(figs.queryByText("From your rules and follows")).toBeNull();
    expect(figs.queryByText("Following")).toBeNull();
  });

  it("says the briefing failed in What fired and Data health, never waits on it forever, and Retry reads it again", async () => {
    let calls = 0;
    api.briefing = () => (++calls === 1 ? Promise.reject(new ApiError(500, "boom")) : Promise.resolve({ status: "read", count: 2, new_since_visit: 0, groups: [{ event_type: "po_late", count: 2, in_top: 0, new_since_visit: 0, label: "Late purchase order" }], items: [] }));
    render(<WatchingPage />);
    const fired = (await screen.findByRole("heading", { name: "What fired" })).closest("section")!;
    const retry = await within(fired).findByRole("button", { name: /try again|retry/i });
    expect(within(fired).queryByText(/Reading what fired/)).toBeNull();
    await userEvent.click(retry);
    await waitFor(() => expect(figure("Firing now")).toBe("2"));
    expect(calls).toBe(2);
  });

  it("says the briefing failed on the Data health tab too, never waits on it forever, and Retry reads it again", async () => {
    let calls = 0;
    api.briefing = () => (++calls === 1 ? Promise.reject(new ApiError(500, "boom")) : Promise.resolve({ status: "read", count: 0, new_since_visit: 0, groups: [], items: [] }));
    window.location.hash = "#data-health";
    try {
      render(<WatchingPage />);
      const health = await screen.findByTestId("data-health");
      const retry = await within(health).findByRole("button", { name: /try again|retry/i });
      expect(within(health).queryByText(/Reading data health/)).toBeNull();
      await userEvent.click(retry);
      await vi.waitFor(() => expect(calls).toBe(2));
      await vi.waitFor(() => expect(within(screen.getByTestId("data-health")).queryByRole("button", { name: /try again|retry/i })).toBeNull());
    } finally {
      window.location.hash = "";
    }
  });

  it("says what fired could not be counted when the briefing answers unread, never that nothing fired", async () => {
    api.briefing = () => Promise.resolve({ status: "unavailable", count: 0, new_since_visit: 0, groups: [], items: [] });
    render(<WatchingPage />);
    const fired = (await screen.findByRole("heading", { name: "What fired" })).closest("section")!;
    await within(fired).findByText(/couldn't be counted just now/);
    expect(within(fired).queryByText(/Nothing fired/)).toBeNull();
  });

  it("says when some alerts could not be read instead of claiming there are none", async () => {
    api.briefing = () => new Promise(() => {});
    api.watchlist = { configured: true, alerts: [], follows: [], rules: [], unread: 0, complete: false, not_read: ["alerts_not_read"] };
    render(<WatchingPage />);
    await screen.findByText(/Some of your alerts couldn't be read just now/);
    expect(screen.queryByText(/None yet\. A rule or a follow/)).toBeNull();
    api.watchlist = { configured: true, alerts: [], follows: [], rules: [], unread: 0, complete: true, not_read: [] };
  });
});

describe("Data health on Watching", () => {
  it("lists each on data-health check with its count, names the off ones, and the kind's label links to it", async () => {
    const reg = {
      rule: "",
      kinds: [{ kind: "data_health", label: "Records to fix", surface: "data_health" }],
      types: [
        { type: "duplicate_party_candidate", kind: "data_health", label: "Customer or supplier entered twice", default: "on", home: false, precision: null, roles: [] },
        { type: "sync_mismatch", kind: "data_health", label: "Record differs in its other system", default: "on", home: false, precision: null, roles: [] },
        { type: "source_stale", kind: "data_health", label: "Source not updated lately", default: "off", home: false, precision: null, roles: [] },
      ],
    };
    const mod = await import("@/lib/api");
    const spy = vi.spyOn(mod, "getAlertKinds").mockResolvedValue(reg as never);
    api.watchlist = { configured: true, alerts: [], follows: [], rules: [], unread: 0 };
    api.briefing = () => Promise.resolve({ status: "read", count: 3, new_since_visit: 0, groups: [{ event_type: "duplicate_party_candidate", count: 3, in_top: 0, new_since_visit: 0, label: "x" }], items: [] });
    window.location.hash = "#data-health";
    const tab = render(<WatchingPage />);
    const row = await screen.findByTestId("data-health-duplicate_party_candidate");
    expect(row).toHaveTextContent("3");
    expect(screen.getByTestId("data-health-sync_mismatch")).toHaveTextContent("None");
    expect(screen.getByTestId("data-health-off")).toHaveTextContent("source not updated lately");
    // the Data health tab is its own view: no alert figures, no What fired
    expect(screen.queryByTestId("watching-figures")).toBeNull();
    expect(screen.queryByRole("heading", { name: "What fired" })).toBeNull();
    tab.unmount();
    window.location.hash = "";
    // the Alerts tab: the records to fix are neither listed, counted nor pointed to (Admin holds them)
    render(<WatchingPage />);
    expect(await screen.findByText("Nothing fired on the alert kinds you can see.")).toBeInTheDocument();
    expect(screen.queryByTestId("health-elsewhere")).toBeNull();
    expect(screen.getByTestId("surface-data_health")).toHaveAttribute("href", "/?show=alerts#data-health");
    expect(screen.queryByTestId("data-health")).toBeNull();
    expect(screen.getByText("Nothing fired on the alert kinds you can see.")).toBeInTheDocument();
    // zero counters are not drawn
    expect(screen.queryByText("Firing now")).toBeNull();
    expect(screen.queryByText("Kinds firing")).toBeNull();
    spy.mockRestore();
  });
});
