import { act, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { setViewingAs } from "@/lib/view-as";
import { MyWorkList } from "./my-work";
import type { MyWork } from "@/lib/types";

const empty = { items: [], digest: { open: 0, due_today: 0, past_due: 0 } } as unknown as MyWork;

describe("screens read as the person viewed", () => {
  afterEach(() => act(() => setViewingAs(null)));

  it("says 'you' in the owner's own view and the name while viewing as someone", () => {
    const { unmount } = render(<MyWorkList work={empty} />);
    expect(screen.getByText("Nothing is waiting on you.")).toBeTruthy();
    unmount();
    act(() => setViewingAs({ key: "0123456789abcdef0123", name: "Vince" }));
    render(<MyWorkList work={empty} />);
    expect(screen.getByText("Nothing is waiting on Vince.")).toBeTruthy();
    expect(screen.queryByText(/waiting on you/)).toBeNull();
  });
});
