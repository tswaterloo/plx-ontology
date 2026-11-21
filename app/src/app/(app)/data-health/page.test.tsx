import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({ usePathname: () => "/data-health", useSearchParams: () => new URLSearchParams(), useRouter: () => ({ replace: () => undefined, push: () => undefined }) }));
vi.mock("@/lib/api", async (orig) => ({
  ...(await orig<typeof import("@/lib/api")>()),
  getDataHealth: () => Promise.resolve({ status: "unread", checks: [], partial: false }),
}));

import DataHealthPage from "./page";

describe("Admin, Data health", () => {
  it("is a page of Admin, titled Data health, never Home's Alerts tab", async () => {
    render(<DataHealthPage />);
    expect(await screen.findByTestId("data-health-page")).toBeInTheDocument();
    expect(screen.getAllByText("Data health").length).toBeGreaterThan(0);
    expect(screen.queryByText("New rule")).toBeNull();
    // a read that failed is said with a retry, never a list of "None"
    expect(await screen.findByText(/couldn.t be read just now/)).toBeInTheDocument();
    expect(screen.queryByText("None")).toBeNull();
  });
});
