import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

const nav = vi.hoisted(() => ({ push: vi.fn() }));
vi.mock("next/navigation", () => ({ useRouter: () => nav }));
vi.mock("@/lib/api", async (orig) => ({
  ...(await orig<typeof import("@/lib/api")>()),
  suggest: (q: string) =>
    Promise.resolve({
      query: q,
      suggestions: /^\d+$/.test(q)
        ? [
            { object_id: "PurchaseOrder:demo:90412", type: "PurchaseOrder", label: "PO 90412", sublabel: "Purchase order", match: q === "90412" ? "exact" : "prefix", score: 1, open_url: "/object/PurchaseOrder/PurchaseOrder%3Ademo%3A90412" },
            { object_id: "PurchaseOrder:demo:90455", type: "PurchaseOrder", label: "PO 90455", sublabel: "Purchase order", match: "prefix", score: 0.8, open_url: "/object/PurchaseOrder/PurchaseOrder%3Ademo%3A90455" },
          ]
        : [{ object_id: "Item:demo:lav", type: "Item", label: "Lavender bar", sublabel: "Item", match: "fuzzy", score: 0.4, open_url: "/object/Item/Item%3Ademo%3Alav" }],
    }),
}));

import { CommandPaletteProvider, useCommandPalette } from "./command-palette";

function Opener() {
  const { open } = useCommandPalette();
  return (
    <button type="button" onClick={open}>
      open
    </button>
  );
}

async function typeAndEnter(text: string, settled: RegExp) {
  render(
    <CommandPaletteProvider>
      <Opener />
    </CommandPaletteProvider>,
  );
  await userEvent.click(screen.getByRole("button", { name: "open" }));
  await userEvent.type(await screen.findByTestId("palette-input"), text);
  await screen.findByText(settled);
  await waitFor(() => expect(screen.queryByRole("status", { name: "Searching" })).toBeNull());
  await userEvent.keyboard("{Enter}");
}

afterEach(() => nav.push.mockClear());

describe("the command bar's Enter", () => {
  it("opens the record whose number was typed exactly", async () => {
    await typeAndEnter("90412", /PO 90412/);
    expect(nav.push).toHaveBeenCalledWith("/object/PurchaseOrder/PurchaseOrder%3Ademo%3A90412");
  });

  it("goes to the page whose name was typed", async () => {
    await typeAndEnter("late", /Lavender bar/);
    expect(nav.push).toHaveBeenCalledWith("/late");
  });

  it("asks anything else, even with records that only partly match", async () => {
    await typeAndEnter("904", /PO 90455/);
    expect(nav.push).toHaveBeenCalledWith("/ask?q=904");
  });

  it("asks a plain question of everything, even on a record page", async () => {
    window.history.pushState({}, "", "/object/PurchaseOrder/PurchaseOrder%3A90412");
    try {
      await typeAndEnter("what ships next week", /Lavender bar/);
      expect(nav.push).toHaveBeenLastCalledWith("/ask?q=what%20ships%20next%20week");
    } finally {
      window.history.pushState({}, "", "/");
    }
  });

  it("takes the open record along for an instruction", async () => {
    window.history.pushState({}, "", "/object/PurchaseOrder/PurchaseOrder%3A90412");
    try {
      await typeAndEnter("chase the supplier", /Lavender bar/);
      expect(nav.push).toHaveBeenLastCalledWith("/ask?q=chase%20the%20supplier&object=PurchaseOrder%3A90412");
    } finally {
      window.history.pushState({}, "", "/");
    }
  });
});
