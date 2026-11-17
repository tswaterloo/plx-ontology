import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ApiError } from "@/lib/errors";

const nav = vi.hoisted(() => ({ view: null as string | null }));
const api = vi.hoisted(() => ({ daily: null as null | (() => Promise<unknown>) }));
vi.mock("next/navigation", () => ({
  useSearchParams: () => new URLSearchParams(nav.view ? `view=${nav.view}` : ""),
  useRouter: () => ({ replace: () => undefined, push: () => undefined }),
  usePathname: () => "/qc",
}));
vi.mock("@/lib/api", async (orig) => ({
  ...(await orig<typeof import("@/lib/api")>()),
  getQcDaily: () => api.daily!(),
  getQcAudit: () => api.daily!(),
}));

import QcPage from "./page";

const brief = (sections: unknown[]) => ({ kind: "qc_daily", title: "QC daily pack", as_of: "2030-01-02T12:00:00Z", sections, basis: "cited" });

describe("Quality page", () => {
  it("shows the daily pack's sections, the hold-release queue among them", async () => {
    api.daily = () => Promise.resolve(brief([{ key: "holds", title: "Hold-release queue", state: "shown", rows: [{ label: "Batch 12 on hold", sources: [] }] }]));
    render(<QcPage />);
    expect(await screen.findByText("Hold-release queue")).toBeInTheDocument();
  });

  it("a reader with no quality grant is told so once, not shown empty sections", async () => {
    api.daily = () => Promise.resolve(brief([{ key: "a", title: "Open checks", state: "quality_marked" }, { key: "b", title: "Holds", state: "quality_marked" }]));
    render(<QcPage />);
    expect(await screen.findByTestId("qc-held-back")).toHaveTextContent("quality control");
    expect(screen.queryByText("Open checks")).toBeNull();
  });

  it("a failed read says so with a retry, and the audit tab offers the text download", async () => {
    api.daily = () => Promise.reject(new ApiError(500, "internal"));
    render(<QcPage />);
    expect(await screen.findByRole("button", { name: /try again/i })).toBeInTheDocument();
    nav.view = "audit";
    api.daily = () => Promise.resolve({ ...brief([{ key: "x", title: "On time by area", state: "shown", rows: [] }]), kind: "qc_audit" });
    render(<QcPage />);
    expect(await screen.findByTestId("qc-audit-download")).toBeInTheDocument();
    nav.view = null;
  });
});
