import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { WhyDidItChange } from "./why";
import * as api from "@/lib/api";

afterEach(() => vi.restoreAllMocks());

describe("why did this change", () => {
  it("shows each step as it is read and keeps the answer so far when stopped", async () => {
    let finish: () => void = () => undefined;
    vi.spyOn(api, "streamWhy").mockImplementation((_id, on, signal) => new Promise<void>((resolve, reject) => {
      on({ step: 1, of: 3, says: "How Revenue changed", answer: "Revenue: 55 now against 5 before, a change of 50" });
      signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
      finish = resolve;
    }));
    render(<WhyDidItChange id="measure_revenue" testId="why" />);
    fireEvent.click(screen.getByText("Why did this change?"));
    await waitFor(() => expect(screen.getByTestId("why-step-1")).toBeTruthy());
    expect(screen.getByTestId("why-answer").textContent).toContain("So far: Revenue: 55 now against 5 before");
    fireEvent.click(screen.getByText("Answer now"));
    await waitFor(() => expect(screen.getByTestId("why-answer").textContent).toContain("Answer so far: Revenue: 55"));
    expect(screen.queryByText("Answer now")).toBeNull();
    finish();
  });

  it("says where it stopped when a step could not be read", async () => {
    vi.spyOn(api, "streamWhy").mockImplementation(async (_id, on) => {
      on({ step: 1, of: 3, says: "How Revenue changed", answer: "Revenue: up" });
      on({ step: 2, of: 3, says: "Which dimension holds the change", answer: "Revenue: up", error: { code: "insight_too_slow", message: "slow", status: 504 } });
    });
    render(<WhyDidItChange id="measure_revenue" testId="why" />);
    fireEvent.click(screen.getByText("Why did this change?"));
    await waitFor(() => expect(screen.getByText(/It stopped at step 2/)).toBeTruthy());
  });
});

describe("why: every dimension ranked, each with its parts", () => {
  it("shows each ranked dimension with its figures and reads its parts on the click", async () => {
    const calls: unknown[] = [];
    vi.spyOn(api, "streamWhy").mockImplementation(async (_id, on, _s, ask) => {
      calls.push(ask);
      if (ask?.drill) {
        on({ step: 1, of: 1, says: "parts", columns: ["supplier", "change"], rows: [["DSM", "-40"]], answer: "" });
        return;
      }
      on({ step: 2, of: 3, says: "Which dimension holds the change", answer: "x", ranking: [
        { dimension: "supplier", parts: "10", parts_for_half: "1", top_part: "DSM", top_change: "-40", top_share_of_change_pct: "80.0", whole_change: "-50", unknown_parts: "0", concentration_pct: "10.0" },
        { dimension: "category", parts: "4", parts_for_half: "2", top_part: "Salt", top_change: "-20", top_share_of_change_pct: "40.0", whole_change: "-50", unknown_parts: "0", concentration_pct: "50.0" },
      ] });
    });
    render(<WhyDidItChange id="measure_purchases" testId="why" />);
    fireEvent.click(screen.getByText("Why did this change?"));
    await waitFor(() => expect(screen.getByTestId("why-ranking")).toBeTruthy());
    expect(screen.getByTestId("why-ranking").textContent).toContain("DSM");
    expect(screen.getByTestId("why-ranking").textContent).toContain("category");
    fireEvent.click(screen.getAllByText("Show every part")[0]);
    await waitFor(() => expect(screen.getByTestId("why-parts-supplier").textContent).toContain("DSM"));
    expect(calls[1]).toEqual({ drill: "supplier" });
  });
});
