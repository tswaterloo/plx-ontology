import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { MemoryItem } from "@/lib/types";

const view = vi.hoisted(() => ({ as: null as { key: string; name: string } | null }));
vi.mock("@/components/view-as", () => ({ useViewingAs: () => view.as }));
const api = vi.hoisted(() => ({ getMemory: vi.fn(), doneMemory: vi.fn() }));
vi.mock("@/lib/api", async (orig) => ({ ...(await orig<object>()), ...api }));

import { byDue, dueLine, taskOf, YourCommitments } from "./your-commitments";

const c = (id: number, text: string, due: string | null, at = "2026-11-12T15:00:00Z"): MemoryItem => ({
  id, kind: "commitment", source: "ask", at, text, records: [], importance: 0.85, confidence: 0.8, due,
});

beforeEach(() => {
  view.as = null;
  api.getMemory.mockReset();
  api.doneMemory.mockReset();
});

describe("your commitments", () => {
  it("says the task without how it was asked", () => {
    expect(taskOf("Remind me to call Northwind Friday.")).toBe("Call Northwind Friday");
    expect(taskOf("I'll send Acme the COA")).toBe("Send Acme the COA");
    expect(taskOf("don't let me forget to chase PO 1234")).toBe("Chase PO 1234");
    expect(taskOf("Follow up with Kestrel on the proof")).toBe("Follow up with Kestrel on the proof");
  });

  it("reads due days against today in Eastern and puts overdue first", () => {
    expect(dueLine("2026-11-13", "2026-11-14")).toBe("Overdue since Fri 13 Nov");
    expect(dueLine("2026-11-14", "2026-11-14")).toBe("Due today");
    expect(dueLine("2026-11-15", "2026-11-14")).toBe("Due tomorrow");
    expect(dueLine("2026-11-20", "2026-11-14")).toBe("Due Fri 20 Nov");
    expect(dueLine(null, "2026-11-14")).toBe("");
    const order = byDue([c(1, "a", null, "2026-11-10T00:00:00Z"), c(2, "b", "2026-11-20"), c(3, "c", "2026-11-13"), c(4, "d", null, "2026-11-12T00:00:00Z")]);
    expect(order.map((i) => i.id)).toEqual([3, 2, 4, 1]);
  });

  it("lists only commitments, and Done takes one off", async () => {
    api.getMemory.mockResolvedValue({ available: true, items: [c(1, "Remind me to call Northwind", "2020-01-02"), { ...c(2, "Always cases", null), kind: "instruction" }] });
    api.doneMemory.mockResolvedValue(undefined);
    render(<YourCommitments />);
    expect(await screen.findByText("Call Northwind")).toBeTruthy();
    expect(screen.getByText(/Overdue since/)).toBeTruthy();
    expect(screen.getAllByTestId("commitment-row")).toHaveLength(1);
    expect(screen.getByRole("heading").textContent).toBe("You said you would1, 1 overdue");
    api.getMemory.mockResolvedValue({ available: true, items: [] });
    fireEvent.click(screen.getByTestId("commitment-done"));
    await waitFor(() => expect(api.doneMemory).toHaveBeenCalledWith(1));
    await waitFor(() => expect(screen.queryByTestId("your-commitments")).toBeNull());
  });

  it("shows nothing when memory can't be read, and reads nothing viewing as someone", async () => {
    api.getMemory.mockRejectedValue(new Error("down"));
    const { container } = render(<YourCommitments />);
    await waitFor(() => expect(api.getMemory).toHaveBeenCalled());
    expect(container.innerHTML).toBe("");
    api.getMemory.mockReset();
    view.as = { key: "k", name: "Ana" };
    render(<YourCommitments />);
    expect(api.getMemory).not.toHaveBeenCalled();
  });
});

describe("desktop reminders", () => {
  it("reminds once a day of what is due or overdue, never of the rest", async () => {
    const { dueReminders } = await import("./your-commitments");
    const kept: Record<string, string> = {};
    const store = { getItem: (k: string) => kept[k] ?? null, setItem: (k: string, v: string) => void (kept[k] = v) };
    const morning = new Date("2026-11-14T13:00:00Z");   // 8 AM ET
    const items = [c(1, "Remind me to call Northwind", "2026-11-13"), c(2, "Send Acme the COA today", "2026-11-14"), c(3, "Chase PO 77", "2026-11-20"), c(4, "Check the proof", null), { ...c(5, "x", "2026-11-01"), kind: "fact" as const }];
    const first = dueReminders(items, store, morning);
    expect(first.map((n) => [n.title, n.body])).toEqual([["Overdue: you said you would", "Call Northwind"], ["Due today: you said you would", "Send Acme the COA today"]]);
    expect(dueReminders(items, store, morning)).toEqual([]);
    expect(dueReminders(items, store, new Date("2026-11-15T13:00:00Z")).map((n) => n.body)).toEqual(["Call Northwind", "Send Acme the COA today"]);
    expect(dueReminders(items, null, morning)).toHaveLength(2);
  });
});
