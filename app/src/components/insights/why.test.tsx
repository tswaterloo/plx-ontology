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
