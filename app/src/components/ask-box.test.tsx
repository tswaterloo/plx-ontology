import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AskBox, mentionAt, SUGGEST_DEBOUNCE_MS } from "./ask-box";
import { resetWarmAsk } from "@/lib/api";

const push = vi.fn();
vi.mock("next/navigation", () => ({ useRouter: () => ({ push }) }));

const SUGGESTIONS = {
  query: "northbya",
  source: "fixture",
  took_ms: 1,
  suggestions: [
    { object_id: "Customer:northbay", type: "Customer", label: "Northbay Soap Co.", sublabel: "Toronto", match: "fuzzy", score: 0.7, open_url: "/object/Customer/x" },
    { object_id: "SalesOrder:10433", type: "SalesOrder", label: "SO 10433", sublabel: "Northbay", match: "fuzzy", score: 0.6, open_url: "/object/SalesOrder/y" },
  ],
};

let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  push.mockReset();
  vi.useFakeTimers();
  fetchMock = vi.fn(async (_url: string, _init?: RequestInit) => new Response(JSON.stringify(SUGGESTIONS), { status: 200 }));
  vi.stubGlobal("fetch", fetchMock);
  vi.stubGlobal("requestAnimationFrame", (cb: FrameRequestCallback) => setTimeout(() => cb(0), 0));
});
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

function type(el: HTMLElement, value: string) {
  fireEvent.change(el, { target: { value, selectionStart: value.length } });
}

async function flush() {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(SUGGEST_DEBOUNCE_MS + 10);
  });
}

describe("mentionAt", () => {
  it("is the text after an @ up to the caret, and nothing without one", () => {
    expect(mentionAt("status of @northbya", 19)?.text).toBe("northbya");
    expect(mentionAt("@three ships", 12)?.text).toBe("three ships");
    expect(mentionAt("status of northbya", 18)).toBeNull();
    expect(mentionAt("mail me@x.com now", 17)).toBeNull();
    expect(mentionAt("is @PO-5280 late? northbya", 26)).toBeNull();
    expect(mentionAt("@", 1)?.text).toBe("");
  });
});

describe("AskBox autocomplete", () => {
  it("debounces: fast typing makes one request, for the latest word", async () => {
    render(<AskBox onSubmit={vi.fn()} />);
    const box = screen.getByRole("combobox", { name: "Ask a question" });
    for (const v of ["@n", "@no", "@nor", "@nort", "@northbya"]) type(box, v);
    await flush();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0][0]).toBe("/api/suggest?q=northbya&limit=8");
    expect(screen.getByRole("option", { name: /Northbay Soap Co\./ })).toBeInTheDocument();
    expect(box).toHaveAttribute("aria-expanded", "true");
  });

  it("does not look up one-character words", async () => {
    render(<AskBox onSubmit={vi.fn()} />);
    type(screen.getByRole("combobox"), "@a");
    await flush();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("aborts a slower earlier lookup so stale results never show", async () => {
    const signals: AbortSignal[] = [];
    fetchMock.mockImplementation(async (_u: string, init?: RequestInit) => {
      signals.push(init!.signal!);
      return new Response(JSON.stringify(SUGGESTIONS), { status: 200 });
    });
    render(<AskBox onSubmit={vi.fn()} />);
    const box = screen.getByRole("combobox");
    type(box, "@north");
    await flush();
    type(box, "@northb");
    expect(signals[0]?.aborted).toBe(true);
  });

  it("opens no list and looks nothing up while typing without an @", async () => {
    render(<AskBox onSubmit={vi.fn()} />);
    const box = screen.getByRole("combobox");
    type(box, "plot a chart of northbay sales by month");
    await flush();
    expect(fetchMock).not.toHaveBeenCalled();
    expect(screen.queryByRole("option")).toBeNull();
    expect(box).toHaveAttribute("aria-expanded", "false");
  });

  it("a pick (click or Enter) inserts the record and never navigates; the open icon is the only way to the record", async () => {
    render(<AskBox onSubmit={vi.fn()} />);
    const box = screen.getByRole("combobox") as HTMLTextAreaElement;
    type(box, "show @northbya");
    await flush();
    fireEvent.mouseDown(screen.getByRole("option", { name: /SO 10433/ }));
    expect(push).not.toHaveBeenCalled();
    expect(box.value).toBe("show SO 10433 ");
    type(box, "and @northbyaa");
    await flush();
    fireEvent.keyDown(box, { key: "ArrowDown" });
    fireEvent.keyDown(box, { key: "Enter" });
    expect(push).not.toHaveBeenCalled();
    expect(box.value).toBe("and Northbay Soap Co. ");
    type(box, "@northbyab");
    await flush();
    fireEvent.mouseDown(screen.getAllByTestId("open-record")[1]);
    expect(push).toHaveBeenCalledTimes(1);
    expect(push).toHaveBeenCalledWith("/object/SalesOrder/y");
  });

  it("keeps an unsent question for the tab and clears it on send", () => {
    sessionStorage.clear();
    const onSubmit = vi.fn();
    const { unmount } = render(<AskBox onSubmit={onSubmit} draftKey="t.draft" />);
    type(screen.getByRole("combobox"), "plot sales by month");
    unmount();
    render(<AskBox onSubmit={onSubmit} draftKey="t.draft" />);
    const box = screen.getByRole("combobox") as HTMLTextAreaElement;
    expect(box.value).toBe("plot sales by month");
    fireEvent.keyDown(box, { key: "Enter" });
    expect(sessionStorage.getItem("t.draft")).toBeNull();
  });

  it("works when storage throws", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("off");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("full");
    });
    render(<AskBox onSubmit={vi.fn()} draftKey="t.draft" />);
    type(screen.getByRole("combobox"), "hello");
    expect((screen.getByRole("combobox") as HTMLTextAreaElement).value).toBe("hello");
    vi.restoreAllMocks();
  });

  it("says nothing when a word names no record (a question is not a record search), and opens the list above a box at the bottom", async () => {
    fetchMock.mockImplementation(async () => new Response(JSON.stringify({ ...SUGGESTIONS, suggestions: [] }), { status: 200 }));
    render(<AskBox onSubmit={vi.fn()} dropUp />);
    type(screen.getByRole("combobox"), "you can see the notes from last time probably in @my email");
    await flush();
    expect(screen.queryByText(/No record is named/)).toBeNull();
    expect(screen.queryByRole("status")).toBeNull();
    expect(screen.getByTestId("suggestions").className).toContain("bottom-full");
  });

  it("a question typed while an answer is being written waits, shows as next, and is asked when the answer ends", async () => {
    fetchMock.mockImplementation(async () => new Response(JSON.stringify({ ...SUGGESTIONS, suggestions: [] }), { status: 200 }));
    const onSubmit = vi.fn();
    const onStop = vi.fn();
    const { rerender } = render(<AskBox onSubmit={onSubmit} busy onStop={onStop} />);
    const box = screen.getByRole("combobox") as HTMLTextAreaElement;
    expect(screen.getByRole("button", { name: "Stop answering" })).toBeInTheDocument();
    type(box, "and the one before that?");
    fireEvent.keyDown(box, { key: "Enter" });
    expect(onSubmit).not.toHaveBeenCalled();
    expect(box.value).toBe("");
    expect(screen.getByTestId("ask-queued").textContent).toContain("and the one before that?");
    rerender(<AskBox onSubmit={onSubmit} busy={false} onStop={onStop} />);
    expect(onSubmit).toHaveBeenCalledTimes(1);
    expect(onSubmit.mock.calls[0][0]).toBe("and the one before that?");
    expect(screen.queryByTestId("ask-queued")).toBeNull();
  });

  it("a waiting question can be cancelled", async () => {
    const onSubmit = vi.fn();
    const { rerender } = render(<AskBox onSubmit={onSubmit} busy onStop={vi.fn()} />);
    const box = screen.getByRole("combobox") as HTMLTextAreaElement;
    type(box, "never mind");
    fireEvent.keyDown(box, { key: "Enter" });
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    rerender(<AskBox onSubmit={onSubmit} busy={false} onStop={vi.fn()} />);
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("picks with the keyboard (Tab), inserts the label and sends it as a pinned entity", async () => {
    const onSubmit = vi.fn();
    render(<AskBox onSubmit={onSubmit} />);
    const box = screen.getByRole("combobox") as HTMLTextAreaElement;
    type(box, "why is @northbya");
    await flush();
    fireEvent.keyDown(box, { key: "ArrowDown" });
    expect(box.getAttribute("aria-activedescendant")).toBeTruthy();
    fireEvent.keyDown(box, { key: "Tab" });
    expect(box.value).toBe("why is Northbay Soap Co. ");
    expect(fetchMock).toHaveBeenCalledTimes(1);
    type(box, "why is Northbay Soap Co. late");
    fireEvent.keyDown(box, { key: "Enter" });
    expect(onSubmit).toHaveBeenCalledWith("why is Northbay Soap Co. late", [
      { object_id: "Customer:northbay", type: "Customer", label: "Northbay Soap Co." },
    ], []);
  });

  it("drops a pin whose name was edited out of the question", async () => {
    const onSubmit = vi.fn();
    render(<AskBox onSubmit={onSubmit} />);
    const box = screen.getByRole("combobox");
    type(box, "@northbya");
    await flush();
    fireEvent.keyDown(box, { key: "ArrowDown" });
    fireEvent.keyDown(box, { key: "Tab" });
    type(box, "something else");
    fireEvent.keyDown(box, { key: "Enter" });
    expect(onSubmit).toHaveBeenCalledWith("something else", [], []);
  });

  it("Escape closes the list and Enter then submits", async () => {
    const onSubmit = vi.fn();
    render(<AskBox onSubmit={onSubmit} />);
    const box = screen.getByRole("combobox");
    type(box, "@northbya");
    await flush();
    fireEvent.keyDown(box, { key: "Escape" });
    expect(box).toHaveAttribute("aria-expanded", "false");
    fireEvent.keyDown(box, { key: "Enter" });
    expect(onSubmit).toHaveBeenCalledWith("@northbya", [], []);
  });

  it("a failed lookup shows no list and no error text", async () => {
    fetchMock.mockImplementation(async () => new Response('{"detail":"boom"}', { status: 500 }));
    const { container } = render(<AskBox onSubmit={vi.fn()} />);
    type(screen.getByRole("combobox"), "@northbya");
    await flush();
    expect(screen.queryByRole("option")).toBeNull();
    expect(container.textContent).not.toContain("boom");
  });

  it("never submits an empty question", () => {
    const onSubmit = vi.fn();
    render(<AskBox onSubmit={onSubmit} />);
    const box = screen.getByRole("combobox");
    type(box, "   ");
    fireEvent.keyDown(box, { key: "Enter" });
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("asks with the deeper read only when Deep research is on", () => {
    const onSubmit = vi.fn();
    render(<AskBox onSubmit={onSubmit} deepable />);
    const box = screen.getByRole("combobox");
    type(box, "first");
    fireEvent.keyDown(box, { key: "Enter" });
    expect(onSubmit).toHaveBeenLastCalledWith("first", [], []);
    fireEvent.click(screen.getByRole("switch", { name: /Deep research/ }));
    type(box, "second");
    fireEvent.keyDown(box, { key: "Enter" });
    expect(onSubmit).toHaveBeenLastCalledWith("second", [], [], { deep: true });
  });

  it("turns Deep research off after a send, so each deep question needs a fresh click", () => {
    const onSubmit = vi.fn();
    render(<AskBox onSubmit={onSubmit} deepable />);
    const box = screen.getByRole("combobox");
    fireEvent.click(screen.getByRole("switch", { name: /Deep research/ }));
    type(box, "deep one");
    fireEvent.keyDown(box, { key: "Enter" });
    expect(screen.getByRole("switch")).toHaveAttribute("aria-checked", "false");
    type(box, "plain one");
    fireEvent.keyDown(box, { key: "Enter" });
    expect(onSubmit).toHaveBeenLastCalledWith("plain one", [], []);
  });

  it("sends a queued deep question once, with deep", () => {
    const onSubmit = vi.fn();
    const { rerender } = render(<AskBox onSubmit={onSubmit} deepable busy onStop={vi.fn()} />);
    const box = screen.getByRole("combobox");
    fireEvent.click(screen.getByRole("switch"));
    type(box, "later");
    fireEvent.keyDown(box, { key: "Enter" });
    expect(onSubmit).not.toHaveBeenCalled();
    rerender(<AskBox onSubmit={onSubmit} deepable busy={false} onStop={vi.fn()} />);
    expect(onSubmit).toHaveBeenCalledTimes(1);
    expect(onSubmit).toHaveBeenCalledWith("later", [], [], { deep: true });
  });

  it("warms a session on focus and throttles repeated focus events", async () => {
    resetWarmAsk();
    render(<AskBox onSubmit={vi.fn()} />);
    const box = screen.getByRole("combobox");
    fireEvent.focus(box);
    fireEvent.blur(box);
    fireEvent.focus(box);
    await act(async () => {
      await Promise.resolve();
    });
    const warmCalls = fetchMock.mock.calls.filter(([url]) => url === "/api/ask/warm");
    expect(warmCalls).toHaveLength(1);
    expect(warmCalls[0][1]?.method).toBe("POST");
  });

  it("stops retrying /api/ask/warm for the session after a 403 refusal", async () => {
    resetWarmAsk();
    fetchMock.mockImplementation(async (url: string) =>
      url === "/api/ask/warm"
        ? new Response(JSON.stringify({ error: { code: "canary_route_refused" } }), { status: 403 })
        : new Response(JSON.stringify(SUGGESTIONS), { status: 200 }),
    );
    render(<AskBox onSubmit={vi.fn()} />);
    const box = screen.getByRole("combobox");
    fireEvent.focus(box);
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    // Advance past the 30s cooldown and focus again: must NOT fire a second 403 request.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(35_000);
    });
    fireEvent.blur(box);
    fireEvent.focus(box);
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    const warmCalls = fetchMock.mock.calls.filter(([url]) => url === "/api/ask/warm");
    expect(warmCalls).toHaveLength(1);
  });

  it("phrases alert-condition starters naturally in ask-start", async () => {
    const { riskStarter } = await import("./ask-start");
    expect(riskStarter({ label: "Batch on quality hold", kind: "batch_qc_hold" })).toBe("Which open orders are affected by a batch on quality hold?");
    expect(riskStarter({ label: "Price below cost", kind: "customer_price_below_cost" })).toBe("What needs attention for price below cost?");
    expect(riskStarter({ label: "SO 10433", kind: "order" })).toBe("What is putting SO 10433 at risk?");
  });
});
