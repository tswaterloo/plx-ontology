import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { Brief } from "@/lib/types";
import { BriefView, deltaText, figureText, mondayBefore } from "./brief-view";

const brief = (over: Partial<Brief> = {}): Brief => ({
  kind: "weekly",
  title: "Weekly review, week of 2099-01-05",
  as_of: "2099-01-12",
  basis: "Counted from the records you may read.",
  sections: [
    { key: "figures", title: "This week", state: "shown", rows: [], more: 0, figures: [
      { label: "Orders booked", value: "12", unit: "records", previous: "9", delta: "+3", sources: [] },
      { label: "Invoiced (CAD)", value: "1200.50", unit: "money", currency: "CAD", sources: [] },
      { label: "Shipped on time", value: null, unit: "percent", sources: [] },
    ] },
    { key: "cash", title: "Cash position", state: "finance_marked", rows: [], more: 0, figures: [] },
    { key: "late_pos", title: "Late purchase orders", state: "shown", more: 4, figures: [], rows: [
      { label: "Late purchase order", detail: "PO 1 is 3 days late", sources: [] },
    ] },
  ],
  ...over,
});

describe("brief view", () => {
  it("shows each figure in its unit, a finance-marked section without values, and visible counts only", () => {
    render(<BriefView brief={brief()} eyebrow="Weekly review" />);
    expect(screen.getByText("12")).toBeTruthy();
    expect(screen.getByText("+3 vs 9 the week before")).toBeTruthy();
    expect(screen.getByText("Not read")).toBeTruthy();
    expect(screen.getByText(/Finance-marked/)).toBeTruthy();
    expect(screen.getByText("4 more not listed.")).toBeTruthy();
  });

  it("says an unread or finance-only section once, keeping 'not the same as none', with dates in words", () => {
    render(
      <BriefView
        brief={brief({
          sections: [
            { key: "slips", title: "Top slips", state: "unread", rows: [], more: 0, figures: [], note: "Your alert list could not be read, so this section could not be read: that is not the same as none." },
            { key: "cash", title: "Cash", state: "finance_marked", rows: [], more: 0, figures: [], note: "Finance-marked: shown only to readers who hold finance." },
            { key: "trend", title: "Trend", state: "unread", rows: [], more: 0, figures: [], note: "Week of 2099-01-05 to 2099-01-11." },
          ],
        })}
      />,
    );
    expect(screen.getAllByText(/not the same as none/)).toHaveLength(2); // the slips note once, the trend's empty line
    expect(screen.getAllByText(/Finance-marked/)).toHaveLength(1);
    expect(screen.getByRole("heading", { level: 1 })).not.toHaveTextContent("2099-01-05");
    expect(screen.getByText(/^Week of 5 Jan/)).toBeTruthy();
  });

  it("says a reason shared by sections in a row once, then points at it; a missing figure says so once", () => {
    const why = "Your alert list could not be read, so this section could not be read: that is not the same as none.";
    render(
      <BriefView
        brief={brief({
          when: "2099-01-05",
          title: "Weekly review, week of 2099-01-05",
          sections: [
            { key: "figs", title: "This week", state: "partial", rows: [], more: 0, figures: [{ label: "Orders booked", value: null, unit: "records", note: "could not be read", sources: [] }] },
            { key: "late", title: "Late", state: "unread", rows: [], more: 0, figures: [], note: why },
            { key: "slips", title: "Top slips", state: "unread", rows: [], more: 0, figures: [], note: why },
            { key: "alerts", title: "Top alerts", state: "unread", rows: [], more: 0, figures: [], note: why },
            { key: "cash", title: "Cash", state: "finance_marked", rows: [], more: 0, figures: [], note: "Finance-marked: shown only to readers who hold finance." },
            { key: "margin", title: "Margin", state: "finance_marked", rows: [], more: 0, figures: [], note: "Finance-marked: shown only to readers who hold finance." },
          ],
        })}
      />,
    );
    expect(screen.getAllByText(why)).toHaveLength(1);
    expect(screen.getAllByText("Not read either, for the same reason.")).toHaveLength(2);
    expect(screen.getAllByText(/^Finance-marked/)).toHaveLength(2);
    expect(screen.getByTestId("brief-section-margin")).toHaveTextContent(/^MarginFinance-marked too\.$/);
    expect(screen.getByTestId("brief-figure")).toHaveTextContent(/^Orders bookedNot read$/);
    // the week's date is in the title, so the line under it gives only when it was read
    expect(screen.getByTestId("brief").textContent).not.toMatch(/5 Jan 2099 · read/);
  });

  it("words percents, money and the week picker's Mondays", () => {
    expect(figureText({ value: "92.5", unit: "percent", currency: null })).toBe("92.5%");
    expect(deltaText({ delta: "-2.5", previous: "95.0", unit: "percent", currency: null })).toBe("-2.5 pts vs 95.0% the week before");
    expect(deltaText({ delta: null, previous: "1", unit: "records", currency: null })).toBeNull();
    expect(mondayBefore(new Date(2026, 10, 8), 1)).toBe("2026-10-26");
    expect(mondayBefore(new Date(2026, 10, 9), 0)).toBe("2026-11-09");
  });

  it("lists lead-tier parties apart, linking to their own brief", () => {
    render(<BriefView brief={brief({ kind: "meeting", parties: [], possible_parties: [{ object_id: "Supplier:x:1", type: "Supplier", label: "Lead Co", open_url: "/object/Supplier/x" }] })} eyebrow="Meeting prep" />);
    expect(screen.getByText(/matched by email domain only/)).toBeTruthy();
    expect(screen.getByText("Lead Co").getAttribute("href")).toBe("/meetings/brief/Supplier/Supplier%3Ax%3A1");
  });
});
