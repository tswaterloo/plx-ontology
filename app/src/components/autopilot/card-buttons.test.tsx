import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

const calls = vi.hoisted(() => ({ decided: [] as unknown[], lines: [] as string[] }));
vi.mock("@/lib/api", async (orig) => ({
  ...(await orig<typeof import("@/lib/api")>()),
  decideAutopilotItem: (id: string, body: unknown) => (calls.decided.push([id, body]), Promise.resolve({ state: "snoozed" })),
  replyFromLine: (id: string, line: string) => (calls.lines.push(`${id}:${line}`), Promise.resolve({ state: "waiting" })),
}));

import { PreparedView } from "@/components/autopilot/prepared";

const item = (over: Record<string, unknown> = {}) => ({ item_id: "a".repeat(24), sender: "jordan@x.example", channel: "mail", subject: "Disk", message: "The disk is done.", kind: "your_call", to: [], cc: [], body: "", edited: false, ask: "What should the reply say?", facts: [], summary: "", note: "", plan_status: "drafted", plan_at: "2026-11-24T14:00:00Z", state: "open", created_at: "2026-11-24T14:00:00Z", ...over }) as never;

afterEach(() => {
  vi.useRealTimers();
  calls.decided = [];
  calls.lines = [];
});

describe("The buttons of a prepared card", () => {
  it("Later puts the item off to tomorrow and removes the card", async () => {
    const gone = vi.fn();
    render(<PreparedView items={[item()]} empty="none" onGone={gone} onChanged={() => undefined} />);
    fireEvent.click(screen.getByRole("button", { name: "Later" }));
    await waitFor(() => expect(gone).toHaveBeenCalledWith("a".repeat(24)));
    expect(calls.decided[0]).toMatchObject(["a".repeat(24), { decision: "snooze" }]);
  });
  it("Write the reply sends the line and, with no text yet, says the reply is being written", async () => {
    render(<PreparedView items={[item()]} empty="none" onGone={() => undefined} onChanged={() => undefined} />);
    fireEvent.change(screen.getByLabelText(/What do you want to say/), { target: { value: "ok, tonight" } });
    fireEvent.click(screen.getByRole("button", { name: "Write the reply" }));
    await waitFor(() => expect(screen.getByText(/Writing the reply from your line/)).toBeInTheDocument());
    expect(screen.getByTestId("reply-writing-status")).toHaveTextContent("Still writing (0s)");
    expect(calls.lines).toEqual(["a".repeat(24) + ":ok, tonight"]);
  });
  it("shows still writing with elapsed time while polling and stops once the job ends with a draft or failure", async () => {
    vi.useFakeTimers();
    const onChanged = vi.fn();
    const { rerender } = render(<PreparedView items={[item({ plan_status: "needs_you" })]} empty="none" onGone={() => undefined} onChanged={onChanged} />);
    fireEvent.change(screen.getByLabelText(/What do you want to say/), { target: { value: "approve tonight" } });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Write the reply" }));
    });
    expect(screen.getByTestId("reply-writing-status")).toHaveTextContent("Still writing (0s)");
    const callsAfterClick = onChanged.mock.calls.length;

    // Server reports plan_status = "working" and time ticks
    rerender(<PreparedView items={[item({ plan_status: "working" })]} empty="none" onGone={() => undefined} onChanged={onChanged} />);
    await act(async () => {
      vi.advanceTimersByTime(65_000);
    });
    expect(screen.getByTestId("reply-writing-status")).toHaveTextContent("Still writing (1m 5s)");
    expect(onChanged.mock.calls.length).toBeGreaterThan(callsAfterClick);

    // Job ends with failure (no body arrives): polling MUST stop and status must say so
    rerender(<PreparedView items={[item({ plan_status: "failed", plan_reason: "no_draft", plan_at: "2026-11-24T14:02:00Z" })]} empty="none" onGone={() => undefined} onChanged={onChanged} />);
    expect(screen.queryByTestId("reply-writing-status")).toBeNull();
    expect(screen.getByText(/Could not write the reply \(no_draft\)/)).toBeInTheDocument();
    const callsAfterEnd = onChanged.mock.calls.length;
    await act(async () => {
      vi.advanceTimersByTime(20_000);
    });
    expect(onChanged.mock.calls.length).toBe(callsAfterEnd);
  });
  it("stops polling when the job finishes with a new reply body", async () => {
    vi.useFakeTimers();
    const onChanged = vi.fn();
    const { rerender } = render(<PreparedView items={[item({ plan_status: "needs_you" })]} empty="none" onGone={() => undefined} onChanged={onChanged} />);
    fireEvent.change(screen.getByLabelText(/What do you want to say/), { target: { value: "ship Friday" } });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Write the reply" }));
    });
    rerender(<PreparedView items={[item({ plan_status: "drafted", kind: "reply", body: "We will ship Friday.", plan_at: "2026-11-24T14:01:00Z" })]} empty="none" onGone={() => undefined} onChanged={onChanged} />);
    expect(screen.queryByTestId("reply-writing-status")).toBeNull();
    expect(screen.getByText("We will ship Friday.")).toBeInTheDocument();
    const callsAfterDone = onChanged.mock.calls.length;
    await act(async () => {
      vi.advanceTimersByTime(20_000);
    });
    expect(onChanged.mock.calls.length).toBe(callsAfterDone);
  });
});
