import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

const asked = vi.hoisted(() => ({ pages: [] as Array<[string, number]>, fail: false, first: 0 }));
const item = (n: number) => ({ id: `g${n}`, object_id: `Customer:x:${n}`, object_type: "Customer", summary: `finding ${n}`, open_url: `/object/Customer/Customer%3Ax%3A${n}` });
vi.mock("@/lib/api", async (orig) => ({
  ...(await orig<typeof import("@/lib/api")>()),
  getDataHealth: () => { asked.first += 1; return Promise.resolve({ status: "read", partial: false, checks: [{ type: "dup", label: "Customers entered twice", on: true, read: false, open: 5, count: 0, hidden: 0, more: 0, items: [] }] }); },
  getDataHealthPage: (check: string, offset: number) => {
    asked.pages.push([check, offset]);
    if (asked.fail) return Promise.reject(new Error("down"));
    return Promise.resolve({ status: "read", partial: false, checks: [{ type: "dup", label: "x", on: true, read: true, open: 5, count: 5, hidden: 0, more: offset === 0 ? 2 : 0, items: offset === 0 ? [item(0), item(1), item(2)] : [item(2), item(3), item(4)] }] });
  },
}));

import { DataHealthSection } from "./alert-kinds";

describe("Data health findings", () => {
  it("page on from where the list ended, never list a finding twice, and download as a spreadsheet", async () => {
    render(<DataHealthSection titled={false} />);
    const check = await screen.findByTestId("data-health-dup");
    // the first read only counts: nothing of the findings is read until the check is opened
    expect(screen.getByTestId("data-health-count-dup")).toHaveTextContent("5");
    expect(asked.pages).toEqual([]);
    await userEvent.click(check.querySelector("summary") as HTMLElement);
    await waitFor(() => expect(check.querySelectorAll("li").length).toBe(3));
    expect(screen.getByText(/2 not shown/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /Show 2 more/ }));
    await waitFor(() => expect(check.querySelectorAll("li").length).toBe(5));
    // the page overlaps the last row listed: it is not listed twice
    expect(asked.pages).toEqual([["dup", 0], ["dup", 3]]);
    expect(screen.queryByRole("button", { name: /more/ })).toBeNull();
    expect(screen.getByRole("link", { name: /Download this check/ })).toHaveAttribute("href", "/api/data-health/export?check=dup");
    expect(screen.getByRole("link", { name: /Download all/ })).toHaveAttribute("href", "/api/data-health/export");
  });

  it("a check that cannot be read says so and lists nothing", async () => {
    asked.fail = true;
    render(<DataHealthSection titled={false} />);
    const check = await screen.findByTestId("data-health-dup");
    await userEvent.click(check.querySelector("summary") as HTMLElement);
    expect(await screen.findByRole("alert")).toHaveTextContent(/couldn.t be read/);
    expect(check.querySelectorAll("li").length).toBe(0);
  });
});

describe("Data health assign", () => {
  it("offers Assign on each finding through the card desk with the finding's own card id", async () => {
    asked.fail = false;
    render(<DataHealthSection titled={false} />);
    const check = await screen.findByTestId("data-health-dup");
    await userEvent.click(check.querySelector("summary") as HTMLElement);
    await waitFor(() => expect(check.querySelectorAll("li").length).toBe(3));
    expect(check.querySelectorAll('[data-testid="action-assign"]').length).toBe(3);
  });
});
