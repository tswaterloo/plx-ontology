import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ShowAll } from "./show-all";

describe("ShowAll", () => {
  it("shows the first N, says how many there are and opens every one; no control when nothing is cut", () => {
    for (const n of [1, 3, 10]) {
      const items = Array.from({ length: n }, (_, i) => `R${i}`);
      const { unmount } = render(
        <ul>
          <ShowAll items={items} first={3} as="li" noun="records">
            {(r) => <li>{r}</li>}
          </ShowAll>
        </ul>,
      );
      expect(screen.getAllByText(/^R\d+$/)).toHaveLength(Math.min(n, 3));
      if (n > 3) {
        fireEvent.click(screen.getByRole("button", { name: `Show all ${n} records` }));
        expect(screen.getAllByText(/^R\d+$/)).toHaveLength(n);   // none lost, none twice
        fireEvent.click(screen.getByRole("button", { name: "Show fewer" }));
        expect(screen.getAllByText(/^R\d+$/)).toHaveLength(3);
      } else expect(screen.queryByRole("button")).toBeNull();
      unmount();
    }
  });
});
