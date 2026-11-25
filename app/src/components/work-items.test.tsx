import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { WorkItems } from "./work-items";

const groups = [
  {
    title: "Reply",
    items: [
      { source: "Mail", who: "Dana Reyes", when: "24 Nov, 9:12 AM", text: "Asks for the revised ship date on PO100412.", open_url: "/source?handle=graph-mail%3A%2F%2Fx" },
      { source: "Teams", who: "Sam Ortiz", when: "24 Nov, 11:40 AM", text: "Wants a yes or no on the Friday pickup.", open_url: null },
    ],
  },
  { title: "Decide", items: [] },
  { title: "Follow up", items: [{ source: "Planner", who: "You", when: "due 26 Nov", text: "Send the label proof to the printer.", open_url: null }] },
];

describe("WorkItems", () => {
  it("shows each item with its source, who, when and what is needed, grouped by what to do", () => {
    render(<WorkItems groups={groups} />);
    const rows = screen.getAllByTestId("work-item");
    expect(rows).toHaveLength(3);
    expect(within(rows[0]).getByText("Dana Reyes")).toBeInTheDocument();
    expect(within(rows[0]).getByText("24 Nov, 9:12 AM")).toBeInTheDocument();
    expect(within(rows[0]).getByText("Asks for the revised ship date on PO100412.")).toBeInTheDocument();
    // a group with no items is not shown
    expect(screen.queryByText("Decide")).toBeNull();
    expect(screen.getByRole("heading", { name: /Reply/ })).toBeInTheDocument();
  });
  it("links the source label only where the message or record can be opened", () => {
    render(<WorkItems groups={groups} />);
    const rows = screen.getAllByTestId("work-item");
    expect(within(rows[0]).getByRole("link", { name: "Open the Mail item" })).toHaveAttribute("href", "/source?handle=graph-mail%3A%2F%2Fx");
    expect(within(rows[1]).queryByRole("link")).toBeNull();
    expect(within(rows[1]).getByText("Teams")).toBeInTheDocument();
  });
  it("shows nothing for no items", () => {
    const { container } = render(<WorkItems groups={[{ title: "Reply", items: [] }]} />);
    expect(container).toBeEmptyDOMElement();
  });
});
