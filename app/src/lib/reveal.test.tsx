import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { REVEAL_MS, revealCut, sharedPrefix, useReveal } from "./reveal";

describe("revealCut", () => {
  it("ends at a whole word, never half a figure", () => {
    const text = "Paid CAD 348,250.00 to Acme in October.";
    expect(text.slice(0, revealCut(text, 12))).toBe("Paid CAD 348,250.00");
    expect(revealCut(text, 0)).toBe(0);
    expect(revealCut(text, 999)).toBe(text.length);
  });

  it("shows a table row whole, never a stray piece of one", () => {
    const text = "Top customers:\n\n| # | Customer |\n|---|---|\n| 1 | A1 American |\n| 2 | Sobeys |\nDone.";
    const at = text.indexOf("Sobeys") + 2;
    expect(text.slice(0, revealCut(text, at)).split("\n").pop()).toBe("| 2 | Sobeys |");
  });

  it("measures how far two texts agree", () => {
    expect(sharedPrefix("Sage AP payments", "Sage AP paid")).toBe(10);
    expect(sharedPrefix("", "x")).toBe(0);
  });
});

function Shown({ text, live }: { text: string; live: boolean }) {
  return <p data-testid="shown">{useReveal(text, live)}</p>;
}

describe("useReveal", () => {
  let now = 0;
  let queued: FrameRequestCallback[] = [];
  const tick = (ms: number) =>
    act(() => {
      now += ms;
      const run = queued;
      queued = [];
      run.forEach((f) => f(now));
    });

  beforeEach(() => {
    now = 0;
    queued = [];
    vi.spyOn(performance, "now").mockImplementation(() => now);
    vi.stubGlobal("requestAnimationFrame", (f: FrameRequestCallback) => queued.push(f));
    vi.stubGlobal("cancelAnimationFrame", () => (queued = []));
  });
  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  const answer = "Last month means October 2026. ".repeat(40).trim();

  it("writes a reply that arrived whole out from the top, done within REVEAL_MS", () => {
    render(<Shown text={answer} live />);
    tick(16);
    const early = screen.getByTestId("shown").textContent ?? "";
    expect(early.length).toBeGreaterThan(0);
    expect(early.length).toBeLessThan(answer.length / 2);
    expect(answer.startsWith(early)).toBe(true);
    for (let t = 0; t < REVEAL_MS; t += 16) tick(16);
    expect(screen.getByTestId("shown").textContent).toBe(answer);
  });

  it("shows a saved answer whole at once", () => {
    render(<Shown text={answer} live={false} />);
    expect(screen.getByTestId("shown").textContent).toBe(answer);
  });

  it("goes on from what is shown when the text grows, and from where they agree when it is replaced", () => {
    const { rerender } = render(<Shown text="Sage AP payments" live={false} />);
    rerender(<Shown text={answer} live={false} />);
    // the narration shown is replaced: nothing it does not share stays on screen
    expect(screen.getByTestId("shown").textContent).toBe("");
    tick(16);
    expect(answer.startsWith(screen.getByTestId("shown").textContent ?? "x")).toBe(true);
    for (let t = 0; t < REVEAL_MS; t += 16) tick(16);
    expect(screen.getByTestId("shown").textContent).toBe(answer);
    rerender(<Shown text={answer + " More."} live={false} />);
    expect(screen.getByTestId("shown").textContent).toBe(answer + " More.");
  });
});
