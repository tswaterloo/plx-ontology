import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { hourWords, RecurringView } from "./recurring";

const ROW = { id: "r1", text: "every Monday chase the late POs", label: "Every Monday", kind: "schedule.weekly", params: { weekday: "monday" }, words: "Every Monday", hour: 8, state: "active", created: 1, last_run: null, next_run: 1795352400, last_result: null };
let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
    if ((init?.method ?? "GET") === "DELETE") return new Response(null, { status: 204 });
    if (init?.method === "PATCH") return new Response(JSON.stringify({ ...ROW, state: "paused" }), { status: 200 });
    return new Response(JSON.stringify({ instructions: [ROW] }), { status: 200 });
  });
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => vi.unstubAllGlobals());

const sent = (method: string) => fetchMock.mock.calls.filter(([, init]) => (init?.method ?? "GET") === method);

describe("RecurringView", () => {
  it("lists your recurring instructions and pauses or stops one", async () => {
    await act(async () => { render(<RecurringView />); });
    expect(screen.getByTestId("recurring-row").textContent).toContain("every Monday chase the late POs");
    expect(screen.getByTestId("recurring-row").textContent).toContain("Every Monday at 8 AM");
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Pause" })); });
    expect(String(sent("PATCH")[0][0])).toBe("/api/instructions/recurring/r1");
    expect(JSON.parse(String(sent("PATCH")[0][1].body))).toEqual({ state: "paused" });
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Stop" })); });
    expect(String(sent("DELETE")[0][0])).toBe("/api/instructions/recurring/r1");
  });

  it("words the hour", () => {
    expect([0, 8, 12, 17].map(hourWords)).toEqual(["12 AM", "8 AM", "12 PM", "5 PM"]);
  });
});
