import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

vi.mock("@/lib/api", async (orig) => ({
  ...(await orig<typeof import("@/lib/api")>()),
  getActionsLog: () => new Promise(() => undefined),
}));

import ActivityPage from "./page";

describe("Activity while its journal is read", () => {
  it("says what it is reading in words", () => {
    render(<ActivityPage />);
    expect(screen.getByTestId("activity-pending")).toHaveTextContent("Reading your activity");
  });
});
