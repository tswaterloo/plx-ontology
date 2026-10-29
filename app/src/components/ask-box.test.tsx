import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AskBox, fragmentAt, SUGGEST_DEBOUNCE_MS } from "./ask-box";

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

describe("fragmentAt", () => {
  it("finds the word at the caret, keeping ids whole", () => {
    expect(fragmentAt("status of northbya", 18).text).toBe("northbya");
    expect(fragmentAt("is PO-5280 late", 8).text).toBe("PO-5280");
    expect(fragmentAt("lot L24-0912", 12).text).toBe("L24-0912");
    expect(fragmentAt("hi ", 3).text).toBe("");
  });
});

describe("AskBox autocomplete", () => {
  it("debounces: fast typing makes one request, for the latest word", async () => {
    render(<AskBox onSubmit={vi.fn()} />);
    const box = screen.getByRole("combobox", { name: "Ask a question" });
    for (const v of ["n", "no", "nor", "nort", "northbya"]) type(box, v);
    await flush();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0][0]).toBe("/api/suggest?q=northbya&limit=8");
    expect(screen.getByRole("option", { name: /Northbay Soap Co\./ })).toBeInTheDocument();
    expect(box).toHaveAttribute("aria-expanded", "true");
  });

  it("does not look up one-character words", async () => {
    render(<AskBox onSubmit={vi.fn()} />);
    type(screen.getByRole("combobox"), "a");
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
    type(box, "north");
    await flush();
    type(box, "northb");
    expect(signals[0]?.aborted).toBe(true);
  });

  it("picks with the keyboard, inserts the label and sends it as a pinned entity", async () => {
    const onSubmit = vi.fn();
    render(<AskBox onSubmit={onSubmit} />);
    const box = screen.getByRole("combobox") as HTMLTextAreaElement;
    type(box, "why is northbya");
    await flush();
    fireEvent.keyDown(box, { key: "ArrowDown" });
    expect(box.getAttribute("aria-activedescendant")).toBeTruthy();
    fireEvent.keyDown(box, { key: "Enter" });
    expect(box.value).toBe("why is Northbay Soap Co. ");
    expect(fetchMock).toHaveBeenCalledTimes(1);
    type(box, "why is Northbay Soap Co. late");
    fireEvent.keyDown(box, { key: "Enter" });
    expect(onSubmit).toHaveBeenCalledWith("why is Northbay Soap Co. late", [
      { object_id: "Customer:northbay", type: "Customer", label: "Northbay Soap Co." },
    ]);
  });

  it("drops a pin whose name was edited out of the question", async () => {
    const onSubmit = vi.fn();
    render(<AskBox onSubmit={onSubmit} />);
    const box = screen.getByRole("combobox");
    type(box, "northbya");
    await flush();
    fireEvent.keyDown(box, { key: "ArrowDown" });
    fireEvent.keyDown(box, { key: "Enter" });
    type(box, "something else");
    fireEvent.keyDown(box, { key: "Enter" });
    expect(onSubmit).toHaveBeenCalledWith("something else", []);
  });

  it("Escape closes the list and Enter then submits", async () => {
    const onSubmit = vi.fn();
    render(<AskBox onSubmit={onSubmit} />);
    const box = screen.getByRole("combobox");
    type(box, "northbya");
    await flush();
    fireEvent.keyDown(box, { key: "Escape" });
    expect(box).toHaveAttribute("aria-expanded", "false");
    fireEvent.keyDown(box, { key: "Enter" });
    expect(onSubmit).toHaveBeenCalledWith("northbya", []);
  });

  it("a failed lookup shows no list and no error text", async () => {
    fetchMock.mockImplementation(async () => new Response('{"detail":"boom"}', { status: 500 }));
    const { container } = render(<AskBox onSubmit={vi.fn()} />);
    type(screen.getByRole("combobox"), "northbya");
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
});
