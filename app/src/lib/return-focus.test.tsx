import { act, render, screen } from "@testing-library/react";
import { useState } from "react";
import { describe, expect, it } from "vitest";
import { useReturnFocus } from "./return-focus";

function Harness() {
  const [open, setOpen] = useState(false);
  useReturnFocus(open);
  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>Opener</button>
      {open && (
        <div role="dialog">
          <input aria-label="inside" autoFocus />
          <button type="button" onClick={() => setOpen(false)}>Close</button>
        </div>
      )}
    </>
  );
}

describe("a dialog opened without a trigger", () => {
  it("gives focus back to what had it when it closes", async () => {
    render(<Harness />);
    const opener = screen.getByRole("button", { name: "Opener" });
    opener.focus();
    act(() => opener.click());
    expect(screen.getByLabelText("inside")).toHaveFocus();
    act(() => screen.getByRole("button", { name: "Close" }).click());
    await act(() => new Promise((r) => setTimeout(r, 5)));
    expect(opener).toHaveFocus();
  });

  it("leaves focus alone when the reader went to another page", async () => {
    render(<Harness />);
    const opener = screen.getByRole("button", { name: "Opener" });
    opener.focus();
    act(() => opener.click());
    history.pushState({}, "", "/elsewhere");
    act(() => screen.getByRole("button", { name: "Close" }).click());
    await act(() => new Promise((r) => setTimeout(r, 5)));
    expect(opener).not.toHaveFocus();
    history.pushState({}, "", "/");
  });
});
