import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const getFeedback = vi.fn();
const getAllFeedback = vi.fn();
const getGaps = vi.fn();
const setFeedbackStatus = vi.fn();
const getObserved = vi.fn();
const getMeasureCandidates = vi.fn();
const approveMeasureCandidate = vi.fn();
const dismissMeasureCandidate = vi.fn();
vi.mock("@/lib/api", () => ({
  getFeedback: (...a: unknown[]) => getFeedback(...a),
  getAllFeedback: (...a: unknown[]) => getAllFeedback(...a),
  getGaps: (...a: unknown[]) => getGaps(...a),
  getObserved: (...a: unknown[]) => getObserved(...a),
  setFeedbackStatus: (...a: unknown[]) => setFeedbackStatus(...a),
  getMeasureCandidates: (...a: unknown[]) => getMeasureCandidates(...a),
  approveMeasureCandidate: (...a: unknown[]) => approveMeasureCandidate(...a),
  dismissMeasureCandidate: (...a: unknown[]) => dismissMeasureCandidate(...a),
}));

import { AnswersRight, dayWords, gapWords, thumbs } from "./answers-right";

const entry = (id: string, rating: number, status: string, extra = {}) => ({ id, at: "2026-11-24T15:00:00+00:00", kind: "answer", status, mine: false, rating, question: `Q ${id}`, reader_name: "Ana", ...extra });

beforeEach(() => {
  getFeedback.mockReset().mockResolvedValue({ all_readers: true, total: 3, offset: 0, counts: {}, entries: [] });
  getAllFeedback.mockReset().mockResolvedValue({ all_readers: true, total: 3, offset: 0, counts: {}, entries: [entry("a", 1, "new"), entry("b", -1, "new", { note: "wrong date", expected: "2026-11-20" }), entry("c", -1, "fixed")] });
  getGaps.mockReset().mockResolvedValue({ on: true, gaps: [{ kind: "measure", name: "freight_cost_per_kg", asked: 12, first: "x", last: "y", defined: false }, { kind: "measure", name: "revenue", asked: 2, first: "x", last: "y", defined: true }] });
  setFeedbackStatus.mockReset().mockResolvedValue({ status: "accepted" });
  getObserved.mockReset().mockResolvedValue({ available: true, days: [{ day: "2026-11-24", answered: 40, failed: 2, unavailable: 1, median_ms: 31000, slowest_ms: 120000, median_steps: 9 }], failures: [{ at: "2026-11-24T15:00:00+00:00", question: "why is it slow", error: "internal:abc" }] });
  getMeasureCandidates.mockReset().mockResolvedValue({ can_review: true, total: 0, counts: { pending: 0, approved: 0, dismissed: 0 }, candidates: [] });
});

describe("Are the answers right?", () => {
  it("counts the thumbs, lists the wrong answers still waiting for a person with what was wrong, and says what was asked for with no definition", async () => {
    render(<AnswersRight />);
    await waitFor(() => expect(screen.getByTestId("answers-thumbs")).toHaveTextContent("1 answers rated right, 2 rated wrong, 1 of those waiting for a person."));
    const waiting = screen.getByTestId("answers-waiting");
    expect(waiting).toHaveTextContent("Q b");
    expect(waiting).toHaveTextContent("What was wrong: wrong date");
    expect(waiting).toHaveTextContent("The right answer is: 2026-11-20");
    expect(waiting).not.toHaveTextContent("Q c");   // already fixed
    expect(screen.getByTestId("answers-gaps")).toHaveTextContent("freight_cost_per_kg (measure): asked 12 times, no definition");
    expect(screen.getByTestId("answers-gaps")).not.toHaveTextContent("revenue");   // defined since: not waiting
    fireEvent.click(screen.getByText("Accept the fix"));
    await waitFor(() => expect(setFeedbackStatus).toHaveBeenCalledWith("b", "accepted"));
    await waitFor(() => expect(screen.queryByTestId("answers-waiting")).toBeNull());
  });
  it("shows nothing at all to a reader who is not the owner or an admin, and says when the gap list is off", async () => {
    getFeedback.mockResolvedValue({ all_readers: false, total: 0, offset: 0, counts: {}, entries: [] });
    const { container } = render(<AnswersRight />);
    await waitFor(() => expect(getFeedback).toHaveBeenCalled());
    expect(container.textContent).toBe("");
    expect(getGaps).not.toHaveBeenCalled();
  });
  it("says the list is not switched on instead of reading as no gaps", async () => {
    getGaps.mockResolvedValue({ on: false, gaps: [] });
    render(<AnswersRight />);
    await waitFor(() => expect(screen.getByTestId("gaps-off")).toBeTruthy());
  });
  it("shows how the Ask has been doing per day and every failed question", async () => {
    render(<AnswersRight />);
    await waitFor(() => expect(screen.getByTestId("answers-observed")).toHaveTextContent("2026-11-24: 40 answered, 2 failed, 1 without the assistant, usually 31 s, slowest 120 s, 9 steps"));
    expect(screen.getByTestId("answers-observed")).toHaveTextContent("2 questions ended in an error".replace("2", "1"));
    getObserved.mockResolvedValue({ available: false, days: [], failures: [] });
    render(<AnswersRight />);
    await waitFor(() => expect(screen.getByTestId("observed-off")).toBeTruthy());
  });
  it("pure: day words", () => {
    expect(dayWords({ day: "d", answered: 3, failed: 0, unavailable: 0, median_ms: 2500, slowest_ms: 2500, median_steps: null })).toBe("3 answered, usually 2.5 s, slowest 2.5 s, cost not kept for this day");
    expect(dayWords({ day: "d", answered: 3, failed: 0, unavailable: 0, median_ms: null, slowest_ms: null, median_steps: null, cost_usd: "1.2500", cost_answers: 2 })).toBe("3 answered, $1.2500 as the runs report it, for 2 of 3 answers");
  });
  it("pure: thumbs and gap words", () => {
    expect(thumbs([{ rating: 1, status: "new" }, { rating: -1, status: "new" }, { rating: -1, status: "accepted" }])).toEqual({ up: 1, down: 2, waiting: 1 });
    expect(gapWords({ kind: "view", name: "v", asked: 1, defined: true })).toBe("v (view): asked once, defined since");
  });
});
