import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";
import type { Me } from "@/lib/types";
import { FirstWeek, firstWeekKey } from "./first-week";

const reader = { display_name: "Pat Reader", email: "pat@plx.example", principal: "employee:p1", auth: "entra", data_label: "", dev_mode: false } as Me;

describe("the first-week note", () => {
  beforeEach(() => window.localStorage.clear());

  it("shows once for a new reader and stays closed after Got it", () => {
    const { unmount } = render(<FirstWeek me={reader} />);
    const note = screen.getByTestId("first-week");
    expect(note).toHaveTextContent("Autopilot stays off until you turn it on yourself, one workflow at a time.");
    expect(screen.getByTestId("first-week-help")).toHaveAttribute("href", "/help");
    expect(note).toHaveTextContent("payroll and HR files stay hidden");
    fireEvent.click(screen.getByTestId("first-week-close"));
    expect(screen.queryByTestId("first-week")).toBeNull();
    expect(window.localStorage.getItem(firstWeekKey(reader.principal))).not.toBeNull();
    unmount();
    render(<FirstWeek me={reader} />);
    expect(screen.queryByTestId("first-week")).toBeNull();
  });

  it("never shows to an owner reading with full access, or before sign-in", () => {
    render(<FirstWeek me={{ ...reader, access_label: "Viewing with full-read access" }} />);
    render(<FirstWeek me={{ ...reader, can_view_as: true }} />);
    render(<FirstWeek me={null} />);
    expect(screen.queryByTestId("first-week")).toBeNull();
  });

  it("is per reader: another person on the same browser still sees it", () => {
    window.localStorage.setItem(firstWeekKey("employee:other"), "x");
    render(<FirstWeek me={reader} />);
    expect(screen.getByTestId("first-week")).toBeInTheDocument();
  });
});
