import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Pending } from "./skeleton";

describe("a wait with no limit", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("never stops waiting, and says how long it has been", () => {
    render(<Pending page limit={null} label="Preparing the brief" />);
    act(() => { vi.advanceTimersByTime(10 * 60_000); });
    expect(screen.queryByTestId("pending-still")).toBeNull();
    expect(screen.getByTestId("slow-note").textContent).toMatch(/so far/);
  });

  it("the default wait still stops at its limit", () => {
    render(<Pending label="Reading" />);
    act(() => { vi.advanceTimersByTime(61_000); });
    expect(screen.getByTestId("pending-still")).toBeTruthy();
  });
});
