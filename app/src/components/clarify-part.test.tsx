import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { Answer } from "@/lib/types";
import { AssumptionLine, ClarifyPart } from "./clarify-part";

const asked = {
  answer_id: "a1",
  clarify: {
    question: "Which purchase order do you mean?",
    free_text: true,
    options: [
      { label: "PO 5447", detail: "status: Open; date: 2026-10-01" },
      { label: "PO 5448", detail: null },
    ],
  },
  assumption: { text: "the calendar quarter, July to September", options: [{ label: "the fiscal quarter" }] },
} as unknown as Answer;

describe("asking back", () => {
  it("sends the picked record or the person's own words as a reply to the same answer", () => {
    const onReply = vi.fn();
    render(<ClarifyPart a={asked} onReply={onReply} />);
    fireEvent.click(screen.getByRole("button", { name: /PO 5448/ }));
    expect(onReply).toHaveBeenLastCalledWith("PO 5448", { answer_id: "a1", option: 1 });
    fireEvent.change(screen.getByLabelText("Your answer"), { target: { value: "the one from Azelis" } });
    fireEvent.click(screen.getByRole("button", { name: "Answer" }));
    expect(onReply).toHaveBeenLastCalledWith("the one from Azelis", { answer_id: "a1", text: "the one from Azelis" });
  });

  it("says the assumed reading in one line and offers the other reading", () => {
    const onReply = vi.fn();
    render(<AssumptionLine a={asked} onReply={onReply} />);
    expect(screen.getByTestId("assumption").textContent).toContain("Taken as: the calendar quarter, July to September");
    fireEvent.click(screen.getByRole("button", { name: "the fiscal quarter" }));
    expect(onReply).toHaveBeenCalledWith("the fiscal quarter", { answer_id: "a1", option: 0 });
  });

  it("offers nothing to press while a reply cannot be sent", () => {
    render(<ClarifyPart a={asked} />);
    expect(screen.getByRole("button", { name: /PO 5447/ })).toBeDisabled();
  });
});
