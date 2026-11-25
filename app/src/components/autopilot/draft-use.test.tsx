import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { DraftUse, draftUseOf } from "./draft-use";

describe("DraftUse", () => {
  it("lists what changed the draft in the person's words and never what was only read for it", () => {
    render(
      <DraftUse
        use={draftUseOf({
          used_for_draft: {
            memory: [{ id: 1, kind: "preference", text: "Sign off with Best, Sam" }, { id: 4 }],
            related: [{ id: 2, kind: "preference", text: "Usually acts on prepared items (job approval)" }, { id: 3 }],
            template: { id: 9, name: "Quote reply", doc_type: "quote" },
          },
        })}
      />,
    );
    const applied = screen.getByTestId("draft-use-applied");
    expect(within(applied).getByText("Sign off with Best, Sam")).toBeTruthy();
    expect(within(applied).queryByText(/preference:/)).toBeNull();
    expect(screen.queryByTestId("draft-use-related")).toBeNull();
    expect(screen.queryByText(/Related, not applied|Usually acts on|can't read/)).toBeNull();
    expect(screen.getByText("Quote reply")).toBeTruthy();
  });

  it("folds a long list of applied notes", () => {
    const memory = Array.from({ length: 7 }, (_, n) => ({ id: n + 1, text: `note ${n + 1}` }));
    render(<DraftUse use={{ memory, related: [] }} />);
    expect(screen.queryByText("note 6")).toBeNull();
    fireEvent.click(screen.getByTestId("show-more"));
    expect(screen.getByText("note 7")).toBeTruthy();
  });

  it("renders nothing when the item carries no draft use, or only related entries", () => {
    const { container } = render(<DraftUse use={draftUseOf({ title: "x" })} />);
    expect(container.innerHTML).toBe("");
    const empty = render(<DraftUse use={{ memory: [], related: [{ id: 1, text: "x" }], template: null }} />);
    expect(empty.container.innerHTML).toBe("");
  });
});
