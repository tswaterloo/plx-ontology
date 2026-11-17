import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "@/lib/errors";
import type { MemoryItem } from "@/lib/types";

const view = vi.hoisted(() => ({ as: null as { key: string; name: string } | null }));
vi.mock("@/components/view-as", () => ({ useViewingAs: () => view.as }));
const api = vi.hoisted(() => ({ getMemory: vi.fn(), saveMemory: vi.fn(), forgetMemory: vi.fn(), getMemoryPeople: vi.fn() }));
vi.mock("@/lib/api", async (orig) => ({ ...(await orig<object>()), ...api }));

import MemoryPage from "./page";

const item = (over: Partial<MemoryItem> = {}): MemoryItem => ({
  id: 3, kind: "instruction", source: "ask", at: "2026-11-13T15:00:00Z", text: "Always give quantities in cases.",
  records: ["SalesOrder:5512"], importance: 0.9, confidence: 0.8, ...over,
});

beforeEach(() => {
  view.as = null;
  Object.values(api).forEach((f) => f.mockReset());
  api.getMemoryPeople.mockResolvedValue({ available: true, people: [], threads: [] });
});

describe("Memory", () => {
  it("groups items by kind with where each came from, and links their records", async () => {
    api.getMemory.mockResolvedValue({ available: true, items: [item(), item({ id: 4, kind: "fact", source: "you", text: "M lot codes are Mississauga.", records: [] })] });
    render(<MemoryPage />);
    expect(await screen.findByText("Always give quantities in cases.")).toBeTruthy();
    const headings = screen.getAllByRole("heading", { level: 2 }).map((h) => h.textContent);
    expect(headings).toEqual(["Tell it something to remember", "Standing instructions 1", "Facts 1"]);
    expect(screen.getByText("From a question of yours")).toBeTruthy();
    expect(screen.getByText("In your words")).toBeTruthy();
    expect(screen.getByText("SO 5512").getAttribute("href")).toBe("/object/SalesOrder/SalesOrder%3A5512");
  });

  it("remembers new words as the chosen kind and shows the server's refusal", async () => {
    api.getMemory.mockResolvedValue({ available: true, items: [] });
    api.saveMemory.mockRejectedValueOnce(new ApiError(422, "memory_refused", undefined, "Memory never keeps pay or finance. Keep that out and try again."));
    render(<MemoryPage />);
    fireEvent.click(await screen.findByRole("radio", { name: "Commitments" }));
    fireEvent.change(screen.getByTestId("memory-text"), { target: { value: "Her hourly rate is 30" } });
    fireEvent.click(screen.getByTestId("memory-save"));
    expect(await screen.findByRole("alert")).toHaveProperty("textContent", "Memory never keeps pay or finance. Keep that out and try again.");
    expect(api.saveMemory).toHaveBeenCalledWith({ kind: "commitment", text: "Her hourly rate is 30" }, undefined);
    api.saveMemory.mockResolvedValueOnce(item({ id: 9, kind: "commitment" }));
    fireEvent.change(screen.getByTestId("memory-text"), { target: { value: "Follow up with Northwind Friday" } });
    fireEvent.click(screen.getByTestId("memory-save"));
    await waitFor(() => expect(api.getMemory).toHaveBeenCalledTimes(2));
  });

  it("corrects and forgets one item", async () => {
    api.getMemory.mockResolvedValue({ available: true, items: [item()] });
    api.saveMemory.mockResolvedValue(item({ text: "Always give quantities in cases of 12." }));
    api.forgetMemory.mockResolvedValue(undefined);
    render(<MemoryPage />);
    fireEvent.click(await screen.findByTestId("memory-edit"));
    const box = screen.getAllByTestId("memory-text")[1];
    fireEvent.change(box, { target: { value: "Always give quantities in cases of 12." } });
    fireEvent.click(screen.getAllByTestId("memory-save")[1]);
    await waitFor(() => expect(api.saveMemory).toHaveBeenCalledWith({ kind: "instruction", text: "Always give quantities in cases of 12." }, 3));
    fireEvent.click(await screen.findByTestId("memory-forget"));
    await waitFor(() => expect(api.forgetMemory).toHaveBeenCalledWith(3));
  });

  it("reads nothing while viewing as someone", () => {
    view.as = { key: "k", name: "Ana" };
    render(<MemoryPage />);
    expect(screen.getByText("Memory stays with its person.")).toBeTruthy();
    expect(api.getMemory).not.toHaveBeenCalled();
  });
});
