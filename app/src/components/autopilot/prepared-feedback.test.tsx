import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { PreparedView } from "@/components/autopilot/prepared";
import type { PreparedItem } from "@/lib/api";

const sent = vi.fn();
vi.mock("@/lib/api", async (orig) => ({ ...(await orig<typeof import("@/lib/api")>()), sendItemFeedback: (b: unknown) => sent(b) }));

const mk = (id: string): PreparedItem => ({
  item_id: id, sender: "", channel: "system", subject: `Item ${id}`, message: "m", kind: "alert", to: [], cc: [], body: "", edited: false,
  ask: "", facts: [], created_at: "2026-11-24T14:00:00Z", alert_kind: "qc_hold",
});

afterEach(() => { cleanup(); sent.mockReset(); });

describe("every prepared item takes feedback", () => {
  it.each([1, 10, 60])("draws one right/wrong control per item (%i)", (n) => {
    render(<PreparedView items={Array.from({ length: n }, (_, k) => mk(`a${k}`))} empty="none" onGone={() => undefined} onChanged={() => undefined} />);
    expect(screen.getAllByTestId("prepared-feedback").length).toBe(Math.min(n, 40));
  });
  it("sends right with the item, and wrong with a note", async () => {
    sent.mockResolvedValue({ ok: true });
    render(<PreparedView items={[mk("x1")]} empty="none" onGone={() => undefined} onChanged={() => undefined} />);
    fireEvent.click(screen.getByText("Right"));
    await waitFor(() => expect(screen.getByTestId("prepared-feedback-thanks")).toBeTruthy());
    expect(sent).toHaveBeenCalledWith(expect.objectContaining({ item_id: "x1", verdict: "useful", item_kind: "suggestion" }));
    cleanup();
    render(<PreparedView items={[mk("x2")]} empty="none" onGone={() => undefined} onChanged={() => undefined} />);
    fireEvent.click(screen.getByText("Wrong"));
    fireEvent.change(screen.getByPlaceholderText("What's wrong? (optional)"), { target: { value: "wrong supplier" } });
    fireEvent.click(screen.getByText("Mark wrong"));
    await waitFor(() => expect(sent).toHaveBeenCalledWith(expect.objectContaining({ item_id: "x2", verdict: "wrong", note: "wrong supplier" })));
  });
  it("says so when the save fails and keeps the note", async () => {
    sent.mockRejectedValue(new Error("x"));
    render(<PreparedView items={[mk("x3")]} empty="none" onGone={() => undefined} onChanged={() => undefined} />);
    fireEvent.click(screen.getByText("Wrong"));
    fireEvent.change(screen.getByPlaceholderText("What's wrong? (optional)"), { target: { value: "n" } });
    fireEvent.click(screen.getByText("Mark wrong"));
    await waitFor(() => expect(screen.getByRole("alert")).toBeTruthy());
    expect((screen.getByPlaceholderText("What's wrong? (optional)") as HTMLInputElement).value).toBe("n");
  });
});
