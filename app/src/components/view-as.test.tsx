import { act, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { setViewingAs } from "@/lib/view-as";
import { ReplyList } from "./reply-list";
import type { ReplyTriage } from "@/lib/types";

const empty = { as_of: "2026-11-05", total: 0, sent: 0, writes: 0, lines: [] } as unknown as ReplyTriage;

describe("screens read as the person viewed", () => {
  afterEach(() => act(() => setViewingAs(null)));

  it("says 'your' in the owner's own view and the name while viewing as someone", async () => {
    const { unmount } = render(<ReplyList triage={empty} />);
    expect(await screen.findByText("Nothing is waiting on your reply.")).toBeTruthy();
    unmount();
    act(() => setViewingAs({ key: "0123456789abcdef0123", name: "Vince" }));
    render(<ReplyList triage={empty} />);
    expect(await screen.findByText("Nothing is waiting on Vince's reply.")).toBeTruthy();
    expect(screen.queryByText(/your reply/)).toBeNull();
  });
});
