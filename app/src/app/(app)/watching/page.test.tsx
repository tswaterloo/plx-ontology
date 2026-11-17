import { render, screen } from "@testing-library/react";
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

import WatchingPage from "./page";

const figure = (label: string) => screen.getByText(label).nextElementSibling?.textContent;

describe("Watching figures", () => {
  it("claims no count of kinds firing until what fired has been read", async () => {
    api.briefing = () => new Promise(() => {});
    render(<WatchingPage />);
    await screen.findByText("Late PO");
    expect(figure("Kinds firing")).toBe("·");
    expect(figure("Firing now")).toBe("·");
  });

  it("counts the kinds firing once read", async () => {
    api.briefing = () => Promise.resolve({ status: "read", count: 2, new_since_visit: 0, groups: [{ event_type: "po_late", count: 2, in_top: 0, new_since_visit: 0, label: "Late purchase order" }], items: [] });
    render(<WatchingPage />);
    await screen.findByText("1 of 2");
    expect(figure("Firing now")).toBe("2");
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
    render(<WatchingPage />);
    const row = await screen.findByTestId("data-health-duplicate_party_candidate");
    expect(row).toHaveTextContent("3");
    expect(screen.getByTestId("data-health-sync_mismatch")).toHaveTextContent("None");
    expect(screen.getByTestId("data-health-off")).toHaveTextContent("source not updated lately");
    expect(screen.getByTestId("surface-data_health")).toHaveAttribute("href", "/watching#data-health");
    spy.mockRestore();
  });
});
