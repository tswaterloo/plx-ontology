import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

// /ask?c=<conversation>: a conversation whose read fails says so with a way to try again,
// and a second try that reads it shows its turns
const api = vi.hoisted(() => ({ getWorkThread: vi.fn(), getAskChats: vi.fn(() => Promise.resolve({ available: true, chats: [] })) }));
const thread = vi.hoisted(() => ({ seed: vi.fn() }));
vi.mock("@/lib/api", async (orig) => ({ ...(await orig<object>()), ...api }));
vi.mock("@/components/shell", async (orig) => ({
  ...(await orig<typeof import("@/components/shell")>()),
  loadLate: () => Promise.resolve([]),
  loadHome: () => Promise.resolve({ examples: [], recent_questions: [], freshness: { sources: [] } }),
}));
vi.mock("@/components/ask-box", () => ({ AskBox: () => <div data-testid="ask-box" /> }));
vi.mock("next/navigation", () => ({ useSearchParams: () => new URLSearchParams("c=559a23fcd1231cb2"), useRouter: () => ({ replace: () => undefined, push: () => undefined }) }));
vi.mock("@/lib/thread", () => ({
  askUrlKey: () => "",
  conversationOf: () => undefined,
  useThread: () => ({ turns: [], run: vi.fn(), retry: vi.fn(), followUp: vi.fn(), stop: vi.fn(), reset: vi.fn(), busy: false, seed: thread.seed }),
}));

import AskRoute from "./page";

describe("Ask resuming a conversation", () => {
  it("says a failed read and reads again on Try again", async () => {
    api.getWorkThread.mockRejectedValueOnce(new Error("Server error."));
    api.getWorkThread.mockResolvedValueOnce({ resume: { turns: [{ question: "Show PO 106001", answer: null, withheld: null }] } });
    render(<AskRoute />);
    const notice = await screen.findByRole("alert");
    expect(notice).toHaveTextContent("This conversation couldn't be opened. The Workbench server failed on this request.");
    expect(thread.seed).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: /Try again/ }));
    await vi.waitFor(() => expect(thread.seed).toHaveBeenCalledTimes(1));
    expect(api.getWorkThread).toHaveBeenCalledTimes(2);
    expect(api.getWorkThread.mock.calls[1][0]).toBe("ask:559a23fcd1231cb2");
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("says a conversation that is gone or not the reader's, with no Try again that cannot help", async () => {
    for (const code of ["not_found", "forbidden"]) {
      api.getWorkThread.mockReset();
      api.getWorkThread.mockRejectedValue({ code, message: code });
      const view = render(<AskRoute />);
      const notice = await screen.findByRole("alert");
      expect(notice.textContent).toMatch(/^This conversation couldn't be opened\. \S/);
      expect(notice).not.toHaveTextContent("just now");
      expect(screen.queryByRole("button", { name: /Try again/ })).toBeNull();
      view.unmount();
    }
  });
});
