import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { DraftUse, draftUseOf } from "./draft-use";

describe("DraftUse", () => {
  it("keeps the entries that changed the draft apart from those read but not applied", () => {
    render(
      <DraftUse
        use={draftUseOf({
          used_for_draft: {
            memory: [{ id: 1, kind: "preference", text: "Sign off with Best, Sam" }],
            related: [{ id: 2, kind: "fact", text: "Freightcom quotes take two days" }, { id: 3 }],
            template: { id: 9, name: "Quote reply", doc_type: "quote" },
          },
        })}
      />,
    );
    const applied = screen.getByTestId("draft-use-applied");
    expect(within(applied).getByText("Used for this draft")).toBeTruthy();
    expect(within(applied).getByText("Sign off with Best, Sam")).toBeTruthy();
    expect(within(applied).queryByText(/Freightcom/)).toBeNull();
    const related = screen.getByTestId("draft-use-related");
    expect(within(related).getByText("Related, not applied")).toBeTruthy();
    expect(within(related).getByText("Freightcom quotes take two days")).toBeTruthy();
    expect(within(related).getByText(/1 entry you can't read/)).toBeTruthy();
    expect(screen.getByText("Quote reply")).toBeTruthy();
  });

  it("shows only related entries when nothing was applied, and folds a long list", () => {
    const related = Array.from({ length: 7 }, (_, n) => ({ id: n + 1, text: `note ${n + 1}` }));
    render(<DraftUse use={{ memory: [], related }} />);
    expect(screen.queryByTestId("draft-use-applied")).toBeNull();
    expect(screen.queryByText("note 6")).toBeNull();
    fireEvent.click(screen.getByTestId("show-more"));
    expect(screen.getByText("note 7")).toBeTruthy();
  });

  it("renders nothing when the item carries no draft use", () => {
    const { container } = render(<DraftUse use={draftUseOf({ title: "x" })} />);
    expect(container.innerHTML).toBe("");
    const empty = render(<DraftUse use={{ memory: [], related: [], template: null }} />);
    expect(empty.container.innerHTML).toBe("");
  });
});
