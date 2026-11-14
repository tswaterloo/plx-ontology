import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({ briefing: null as null | (() => Promise<unknown>) }));
vi.mock("@/lib/api", async (orig) => ({
  ...(await orig<typeof import("@/lib/api")>()),
  getWatchlist: () => Promise.resolve({ alerts: [], follows: [], rules: [] }),
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
});
