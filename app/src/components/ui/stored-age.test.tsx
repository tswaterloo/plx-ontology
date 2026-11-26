import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { StoredAge } from "./stored-age";

afterEach(cleanup);

describe("StoredAge", () => {
  it("renders nothing when data is current (at is null)", () => {
    const { container } = render(<StoredAge at={null} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("renders a quiet refreshing status without 'reading it again'", () => {
    render(<StoredAge at={Date.now() - 60_000} />);
    const el = screen.getByTestId("stored-age");
    expect(el).toHaveAttribute("role", "status");
    expect(el.textContent).toMatch(/Refreshing/i);
    expect(el.textContent).not.toMatch(/reading it again/i);
  });
});
