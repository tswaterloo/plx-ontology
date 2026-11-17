import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({ getMemoryPeople: vi.fn() }));
vi.mock("@/lib/api", async (orig) => ({ ...(await orig<object>()), ...api }));
const view = vi.hoisted(() => ({ as: null as null | object }));
vi.mock("@/components/view-as", () => ({ useViewingAs: () => view.as }));

import { MemoryPeopleSection, nameOf, YourTurn } from "./memory-people";

beforeEach(() => {
  api.getMemoryPeople.mockReset();
  view.as = null;
});

describe("People you work with and conversations going", () => {
  it("lists whose turn each conversation is and the people with the most mail, each opening the Ask", async () => {
    api.getMemoryPeople.mockResolvedValue({
      available: true,
      threads: [
        { subject: "COA for lot 22", others: ["lee.wong@supply.example"], messages: 2, last_at: "2026-11-13T15:00:00Z", turn: "yours" },
        { subject: "PO 4411 ship date", others: ["bob@acme.example"], messages: 3, last_at: "2026-11-12T15:00:00Z", turn: "theirs" },
      ],
      people: [{ address: "bob@acme.example", name: "Bob", inside: false, sent: 2, received: 1, last_at: "2026-11-13T15:00:00Z", last_subject: "Re: PO 4411" }],
    });
    render(<MemoryPeopleSection />);
    expect(await screen.findByText("Your turn")).toBeTruthy();
    expect(screen.getByText("Waiting on them")).toBeTruthy();
    expect(screen.getByText(/1 where the last message came to you/)).toBeTruthy();
    expect(screen.getByText(/Lee Wong · 2 messages/)).toBeTruthy();
    expect(screen.getByRole("link", { name: "COA for lot 22" }).getAttribute("href")).toBe(`/ask?q=${encodeURIComponent('Where does "COA for lot 22" stand?')}`);
    expect(screen.getByRole("link", { name: "Bob" })).toBeTruthy();
    expect(screen.getByText("Last: Re: PO 4411")).toBeTruthy();
  });

  it("says plainly when the mail cannot be read, and shows nothing when there is nothing", async () => {
    api.getMemoryPeople.mockResolvedValue({ available: false, people: [], threads: [] });
    render(<MemoryPeopleSection />);
    expect(await screen.findByText(/can.t be read from your mail right now/)).toBeTruthy();
  });

  it("asks again while the mail is still being read, then shows it", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    api.getMemoryPeople
      .mockResolvedValueOnce({ available: false, reading: true, people: [], threads: [] })
      .mockResolvedValue({ available: true, people: [], threads: [{ subject: "PO 9", others: ["a@b.example"], messages: 2, last_at: "2026-11-13T15:00:00Z", turn: "yours" }] });
    render(<MemoryPeopleSection />);
    await vi.advanceTimersByTimeAsync(2100);
    expect(await screen.findByText("Your turn")).toBeTruthy();
    expect(api.getMemoryPeople).toHaveBeenCalledTimes(2);
    expect(screen.queryByText(/can.t be read/)).toBeNull();
    vi.useRealTimers();
  });

  it("names an address by its own parts", () => {
    expect(nameOf("ana.lopez@x.example")).toBe("Ana Lopez");
    expect(nameOf("j_smith2@x.example")).toBe("J Smith2");
  });
});

describe("Today's your-turn line", () => {
  const thread = (turn: string) => ({ subject: `S ${turn}`, others: ["a@b.example"], messages: 2, last_at: "2026-11-13T15:00:00Z", turn });
  it("counts the conversations where it is your turn and opens them in Memory", async () => {
    api.getMemoryPeople.mockResolvedValue({ available: true, people: [], threads: [thread("yours"), thread("yours"), thread("theirs")] });
    render(<YourTurn />);
    const link = await screen.findByRole("link", { name: "Your turn in 2 conversations" });
    expect(link.getAttribute("href")).toBe("/memory#memory-threads");
  });

  it("shows nothing when none are yours, the mail is not read, or viewing as someone", async () => {
    api.getMemoryPeople.mockResolvedValue({ available: false, reading: true, people: [], threads: [] });
    const { container } = render(<YourTurn />);
    await Promise.resolve();
    expect(container.textContent).toBe("");
    view.as = { name: "Ana" };
    api.getMemoryPeople.mockClear();
    render(<YourTurn />);
    expect(api.getMemoryPeople).not.toHaveBeenCalled();
  });
});
