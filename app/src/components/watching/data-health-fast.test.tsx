import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const state = vi.hoisted(() => ({
  pages: [] as Array<[string, number]>,
  paths: [] as string[],
}));

const mkItem = (n: number, objectType = "Customer", summary?: string) => ({
  id: `g-${n}`,
  object_id: `${objectType}:x:${n}`,
  object_type: objectType,
  summary: summary ?? `Duplicate record ${n}`,
  open_url: `/object/${objectType}/${objectType}%3Ax%3A${n}`,
});

vi.mock("@/lib/api", async (orig) => ({
  ...(await orig<typeof import("@/lib/api")>()),
  getDataHealth: () =>
    Promise.resolve({
      status: "read",
      partial: false,
      checks: [
        {
          type: "dup",
          label: "Customers entered twice",
          on: true,
          read: false,
          open: 120,
          count: 0,
          hidden: 0,
          more: 0,
          items: [],
        },
      ],
    }),
  getDataHealthPage: (check: string, offset: number) => {
    state.pages.push([check, offset]);
    const items =
      offset === 0
        ? Array.from({ length: 50 }, (_, i) => mkItem(i, i < 40 ? "Customer" : "Vendor"))
        : Array.from({ length: 50 }, (_, i) => mkItem(offset + i, "Customer"));
    return Promise.resolve({
      status: "read",
      partial: false,
      checks: [
        {
          type: check,
          label: "Customers entered twice",
          on: true,
          read: true,
          open: 120,
          count: 120,
          hidden: 0,
          more: Math.max(0, 120 - offset - items.length),
          items,
        },
      ],
    });
  },
  readPath: (path: string) => {
    state.paths.push(path);
    const items = [mkItem(99, "Vendor", "Acme Supplier duplicate")];
    return Promise.resolve({
      status: "read",
      partial: false,
      checks: [
        {
          type: "dup",
          label: "Customers entered twice",
          on: true,
          read: true,
          open: 120,
          count: 1,
          hidden: 0,
          more: 0,
          items,
        },
      ],
    });
  },
}));

import { DataHealthSection } from "./alert-kinds";

beforeEach(() => {
  state.pages = [];
  state.paths = [];
});

describe("Data health fast pagination and filtering", () => {
  it("shows exact Showing 1-50 of 120 counts beyond 25 rows, paginates cleanly, and filters server-side", async () => {
    render(<DataHealthSection titled={false} />);
    const check = await screen.findByTestId("data-health-dup");
    await userEvent.click(check.querySelector("summary") as HTMLElement);

    await waitFor(() => expect(screen.getByTestId("data-health-showing-dup")).toHaveTextContent("Showing 1-50 of 120"));
    expect(check.querySelectorAll("li").length).toBe(50);

    // Load next 50 rows (1-100 of 120)
    await userEvent.click(screen.getByRole("button", { name: /Show 50 more/ }));
    await waitFor(() => expect(screen.getByTestId("data-health-showing-dup")).toHaveTextContent("Showing 1-100 of 120"));
    expect(check.querySelectorAll("li").length).toBe(100);

    // Search server-side across all rows
    const input = screen.getByTestId("data-health-search-dup");
    await userEvent.type(input, "Acme");
    await userEvent.click(screen.getByRole("button", { name: "Filter" }));

    await waitFor(() => expect(screen.getByTestId("data-health-showing-dup")).toHaveTextContent("Showing 1-1 of 1"));
    expect(state.paths[0]).toContain("/api/data-health?");
    expect(state.paths[0]).toContain("q=Acme");
    expect(screen.getByRole("link", { name: /Download this check/ })).toHaveAttribute(
      "href",
      "/api/data-health/export?check=dup&q=Acme",
    );
  });
});
