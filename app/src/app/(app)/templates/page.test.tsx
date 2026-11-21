import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import TemplatesPage from "./page";

const push = vi.fn();
vi.mock("next/navigation", () => ({ useRouter: () => ({ push, replace: vi.fn() }), useSearchParams: () => new URLSearchParams(), usePathname: () => "/templates" }));
afterEach(() => {
  vi.unstubAllGlobals();
  push.mockReset();
});

const json = (body: unknown) => new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
const list = { available: true, templates: [], doc_types: [], fields: [], share_roles: [] };
const bank = { view: "answer_bank", title: "Answer bank", rows: [], withheld: 0, complete: true };

function serve(learned: unknown) {
  vi.stubGlobal("fetch", vi.fn(async (url: string) => (String(url).includes("/api/templates/learned") ? json(learned) : String(url).includes("/api/templates/answer-bank") ? json(bank) : json(list))));
}

describe("templates learned from your documents", () => {
  it("lists each learned kind and makes one in Ask for the record you name", async () => {
    serve({ status: "read", learned_at: "2026-11-14T09:50:00Z", templates: [{ kind: "quote", label: "Quote", documents: 7, last_sent: "2026-11-02", ask: "make a quote in my usual template for " }] });
    render(<TemplatesPage />);
    const box = await screen.findByTestId("learned");
    expect(box).toHaveTextContent("Learned from your documents");
    expect(box).toHaveTextContent("From 7 of your documents, latest 2026-11-02");
    fireEvent.click(screen.getByTestId("learned-use"));
    fireEvent.click(screen.getByRole("button", { name: "Make in Ask" }));
    expect(push).not.toHaveBeenCalled(); // nothing named yet
    fireEvent.change(screen.getByTestId("learned-record"), { target: { value: "Harbor Naturals" } });
    fireEvent.keyDown(screen.getByTestId("learned-record"), { key: "Enter" });
    expect(push).toHaveBeenCalledWith(`/ask?q=${encodeURIComponent("make a quote in my usual template for Harbor Naturals")}`);
  });

  it("shows nothing learned when none is, or when the people check is unavailable", async () => {
    for (const learned of [{ status: "none", templates: [] }, { status: "unavailable", templates: [{ kind: "quote", label: "Quote", documents: 1, ask: "x" }] }]) {
      serve(learned);
      const { unmount } = render(<TemplatesPage />);
      await screen.findByText("Yours");
      const f = fetch as unknown as { mock: { calls: unknown[][] } };
      await waitFor(() => expect(f.mock.calls.some((c) => String(c[0]).includes("/api/templates/learned"))).toBe(true));
      await act(async () => {});
      expect(screen.queryByTestId("learned")).toBeNull();
      unmount();
    }
  });
});

describe("templates page: the answer bank while it is first read", () => {
  it("keeps saying it is reading, never an empty list, and asks again until the answers are there", async () => {
    const row = { title: "Do you hold a GMP certificate?", detail: "Yes, since 2019", records: [], figures: [] };
    let asked = 0;
    vi.stubGlobal("fetch", vi.fn(async (url: string) => {
      if (String(url).includes("/api/templates/learned")) return json({ status: "none", templates: [] });
      if (!String(url).includes("/api/templates/answer-bank")) return json(list);
      asked += 1;
      return json(asked === 1 ? { ...bank, rows: [], complete: false, reading: true } : { ...bank, rows: [row] });
    }));
    render(<TemplatesPage />);
    await waitFor(() => expect(asked).toBe(1));
    expect(screen.getByText("Reading past questionnaires")).toBeTruthy();
    expect(screen.queryByTestId("answer-bank-empty")).toBeNull();
    await waitFor(() => expect(screen.getByText("Yes, since 2019")).toBeTruthy(), { timeout: 3000 });
    expect(asked).toBe(2);
  });
});

describe("templates not ready on the first read", () => {
  it("reads again by itself and never says templates are not available", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let reads = 0;
    vi.stubGlobal("fetch", vi.fn(async (url: string) => {
      if (String(url).includes("/api/templates/learned")) return json({ status: "none", templates: [] });
      if (String(url).includes("/api/templates/answer-bank")) return json(bank);
      reads += 1;
      return json(reads === 1 ? { ...list, available: false } : list);
    }));
    try {
      render(<TemplatesPage />);
      await waitFor(() => expect(reads).toBe(1));
      expect(screen.queryByText(/not available/)).toBeNull();
      await act(async () => { await vi.advanceTimersByTimeAsync(3100); });
      await waitFor(() => expect(reads).toBe(2));
      expect(await screen.findByTestId("template-new")).toBeInTheDocument();
      expect(screen.queryByText(/not available|couldn't be read/)).toBeNull();
    } finally {
      vi.useRealTimers();
    }
  });
});
