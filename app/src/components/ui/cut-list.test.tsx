import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { CutList } from "./cut-list";
import { StatusLine } from "./status-line";

describe("CutList", () => {
  it("says how many of the exact total are shown and reads the rest through next", async () => {
    const onMore = vi.fn(async () => ({ items: [3, 4], total: 4, next: null }));
    render(
      <CutList list={{ items: [1, 2], total: 4, next: 2 }} noun="orders" onMore={onMore}>
        {(n) => <span key={n}>row {n}</span>}
      </CutList>,
    );
    expect(screen.getByTestId("cut-list-note")).toHaveTextContent("2 of 4 orders shown");
    fireEvent.click(screen.getByRole("button", { name: "Show the other 2" }));
    await waitFor(() => expect(screen.getByText("row 4")).toBeInTheDocument());
    expect(onMore).toHaveBeenCalledWith(2);
    expect(screen.queryByTestId("cut-list-note")).toBeNull();
  });
  it("a whole list says nothing about cutting", () => {
    render(<CutList list={{ items: [1], total: 1, next: null }} onMore={vi.fn()}>{(n) => <span key={n}>row {n}</span>}</CutList>);
    expect(screen.queryByTestId("cut-list-note")).toBeNull();
  });
});

describe("StatusLine", () => {
  it("renders nothing for ok and the reason for a failure", () => {
    const { container } = render(<StatusLine value={{ status: "ok", as_of: null, why: null }} />);
    expect(container).toBeEmptyDOMElement();
    render(<StatusLine value={{ status: "failed", as_of: null, why: "Business Central" }} />);
    expect(screen.getByRole("status")).toHaveTextContent("Reading failed: Business Central");
  });
});
