import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { AskChats } from "@/lib/types";

const api = vi.hoisted(() => ({ getAskChats: vi.fn(), updateAskChat: vi.fn() }));
vi.mock("@/lib/api", async (orig) => ({ ...(await orig<object>()), ...api }));

import { AskChatList, cleanChatTitle, compactAge } from "./ask-chats";

const hoursAgo = (h: number) => new Date(Date.now() - h * 3_600_000).toISOString();
const LIST: AskChats = {
  available: true,
  chats: [
    { conversation_id: "c2", title: "PO 90412 chase", named: true, pinned: true, questions: 3, last_at: hoursAgo(30), running: false },
    { conversation_id: "c1", title: "Which orders are late?", named: false, pinned: false, questions: 1, last_at: new Date(Date.now() - 60_000).toISOString(), running: false },
  ],
};

describe("Ask's chats list", () => {
  beforeEach(() => {
    api.getAskChats.mockReset().mockResolvedValue(LIST);
    api.updateAskChat.mockReset();
  });

  it("lists pinned then recent, each opening its chat, the open one marked", async () => {
    render(<AskChatList current="c1" onNew={() => undefined} />);
    const links = await screen.findAllByRole("link");
    expect(links.map((a) => [a.textContent?.replace(/\d+\s?\w+ ago|\d+h|\d+d/g, "").trim(), a.getAttribute("href")])).toEqual([
      [expect.stringContaining("PO 90412 chase"), "/ask?c=c2"],
      [expect.stringContaining("Which orders are late?"), "/ask?c=c1"],
    ]);
    expect(screen.getByText("Pinned")).toBeInTheDocument();
    expect(links[1]).toHaveAttribute("aria-current", "page");
  });

  it("searches names and questions on the server, and says when nothing matches", async () => {
    render(<AskChatList onNew={() => undefined} />);
    await screen.findByText("PO 90412 chase");
    api.getAskChats.mockResolvedValueOnce({ available: true, chats: [] });
    await userEvent.type(screen.getByLabelText("Search chats"), "croda");
    await waitFor(() => expect(api.getAskChats).toHaveBeenLastCalledWith("croda", expect.anything()));
    expect(await screen.findByText("No chat matches that.")).toBeInTheDocument();
  });

  it("deletes only after the person confirms, and starts a new chat when the open one goes", async () => {
    const onNew = vi.fn();
    api.updateAskChat.mockResolvedValue({ available: true, chats: [LIST.chats[0]] });
    render(<AskChatList current="c1" onNew={onNew} />);
    await screen.findByText("Which orders are late?");
    await userEvent.click(screen.getByLabelText("More for Which orders are late?"));
    await userEvent.click(await screen.findByRole("menuitem", { name: "Delete" }));
    expect(api.updateAskChat).not.toHaveBeenCalled();
    expect(screen.getByText(/Your work log keeps the record/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Delete" }));
    expect(api.updateAskChat).toHaveBeenCalledWith("c1", { deleted: true });
    await waitFor(() => expect(screen.queryByText("Which orders are late?")).toBeNull());
    expect(onNew).toHaveBeenCalled();
  });

  it("renames in place and pins from the menu", async () => {
    api.updateAskChat.mockResolvedValue(LIST);
    render(<AskChatList onNew={() => undefined} />);
    await screen.findByText("Which orders are late?");
    await userEvent.click(screen.getByLabelText("More for Which orders are late?"));
    await userEvent.click(await screen.findByRole("menuitem", { name: "Rename" }));
    const box = screen.getByLabelText("Chat name");
    await userEvent.clear(box);
    await userEvent.type(box, "Late orders{Enter}");
    expect(api.updateAskChat).toHaveBeenCalledWith("c1", { name: "Late orders" });
    await userEvent.click(screen.getByLabelText("More for PO 90412 chase"));
    await act(async () => userEvent.click(await screen.findByRole("menuitem", { name: "Unpin" })));
    expect(api.updateAskChat).toHaveBeenCalledWith("c2", { pinned: false });
  });

  it("puts time in group headers, not on the rows", async () => {
    render(<AskChatList onNew={() => undefined} />);
    await screen.findByText("Which orders are late?");
    expect(screen.getByRole("region", { name: "Today" })).toHaveTextContent("Which orders are late?");
    expect(screen.queryByText(/ago$/)).toBeNull();
  });

  it("folds the same question asked again into one row with a count, the rest one click away", async () => {
    const again = [1, 2, 3].map((n) => ({ conversation_id: `r${n}`, title: "Which open sales orders are late?", named: false, pinned: false, questions: 1, last_at: hoursAgo(n), running: false }));
    api.getAskChats.mockResolvedValue({ available: true, chats: again });
    render(<AskChatList onNew={() => undefined} />);
    await screen.findByText("Which open sales orders are late?");
    expect(screen.getAllByRole("link")).toHaveLength(1);
    await userEvent.click(screen.getByRole("button", { name: /Asked 3 times/ }));
    expect(screen.getAllByRole("link").map((a) => a.getAttribute("href"))).toEqual(["/ask?c=r1", "/ask?c=r2", "/ask?c=r3"]);
  });

  it("reaches every chat of a long list with no cap or show-more wall", async () => {
    const many = Array.from({ length: 150 }, (_, n) => ({ conversation_id: `m${n}`, title: `Question ${n}`, named: false, pinned: false, questions: 1, last_at: hoursAgo(n / 10), running: false }));
    api.getAskChats.mockResolvedValue({ available: true, chats: many });
    render(<AskChatList onNew={() => undefined} />);
    await screen.findByText("Question 0");
    // no IntersectionObserver in this environment: the whole list is drawn
    await waitFor(() => expect(screen.getAllByRole("link")).toHaveLength(150));
    expect(screen.queryByText(/Show 30 more|Show all/)).toBeNull();
  });

  it("shows a crisp spinner icon (zero colored status dots) on a chat that is actively running", async () => {
    api.getAskChats.mockResolvedValue({
      available: true,
      chats: [
        { conversation_id: "c_run", title: "Soapbox shipments", named: false, pinned: false, questions: 1, last_at: hoursAgo(0), running: true },
        LIST.chats[1],
      ],
    });
    render(<AskChatList current="c_run" onNew={() => undefined} />);
    await screen.findByText("Soapbox shipments");
    const indicators = screen.getAllByTestId("chat-running-indicator");
    expect(indicators).toHaveLength(1);
    expect(indicators[0]).toHaveAttribute("aria-label", "Working on an answer");
    expect(screen.queryByTestId("chat-running-dot")).toBeNull();
  });

  it("cleans raw prompts including noisy real-world questions and formats compact right-aligned ages", () => {
    expect(cleanChatTitle("Focus on Finance (general ledger): please show me overdue AR invoices")).toBe("Overdue AR invoices");
    expect(cleanChatTitle("can you list late POs by supplier")).toBe("List late POs by supplier");
    expect(cleanChatTitle("please keep exact", true)).toBe("please keep exact");
    expect(cleanChatTitle("No any other teamas messages? and why can you tell me why stuff was onyl ca")).toBe("Teamas messages?");
    expect(cleanChatTitle("Can you figure out internal company culture score (create one)...")).toBe("Internal company culture score");
    const longClean = cleanChatTitle("Can you please show me all open purchase orders from Croda and explain why they are delayed");
    expect(longClean.length).toBeLessThanOrEqual(52);
    expect(longClean).not.toMatch(/\b(?:and|or|the|for|of|to|why)$/i);
    const now = new Date("2026-11-26T12:00:00Z");
    expect(compactAge("2026-11-26T10:00:00Z", now)).toBe("2h");
    expect(compactAge("2026-11-23T12:00:00Z", now)).toBe("3d");
    expect(compactAge("2026-11-19T12:00:00Z", now)).toBe("1w");
  });

  it("renders a loading skeleton before chats arrive, stratifies into Pinned / Today / Yesterday / Previous 7 days / Previous 30 days / Older newest-first, and highlights active row", async () => {
    let resolveChats!: (v: AskChats) => void;
    api.getAskChats.mockImplementationOnce(() => new Promise<AskChats>((r) => { resolveChats = r; }));
    render(<AskChatList current="today-1" onNew={() => undefined} />);
    expect(screen.getByTestId("ask-chats-skeleton")).toBeInTheDocument();
    await waitFor(() => expect(api.getAskChats).toHaveBeenCalledTimes(1));

    const now = new Date();
    const daysAgo = (d: number, hour = 12) => new Date(now.getFullYear(), now.getMonth(), now.getDate() - d, hour, 0, 0).toISOString();
    const bucketed: AskChats = {
      available: true,
      chats: [
        // Deliberately unsorted to verify newest-first sorting within groups.
        { conversation_id: "old-1", title: "Q1 archive", named: false, pinned: false, questions: 1, last_at: daysAgo(45), running: false },
        { conversation_id: "month-1", title: "Monthly AR summary", named: false, pinned: false, questions: 1, last_at: daysAgo(18), running: false },
        { conversation_id: "week-1", title: "can you check last week margins", named: false, pinned: false, questions: 1, last_at: daysAgo(4), running: false },
        { conversation_id: "yest-1", title: "Yesterday supplier check", named: false, pinned: false, questions: 1, last_at: daysAgo(1), running: false },
        { conversation_id: "today-older", title: "Morning queue", named: false, pinned: false, questions: 1, last_at: daysAgo(0, 8), running: false },
        { conversation_id: "today-1", title: "Focus on Purchasing: please show me today PO check", named: false, pinned: false, questions: 1, last_at: daysAgo(0, 14), running: false },
        { conversation_id: "pin-1", title: "Pinned revenue", named: true, pinned: true, questions: 2, last_at: daysAgo(15), running: false },
      ],
    };
    await act(async () => {
      resolveChats(bucketed);
    });

    expect(await screen.findByRole("region", { name: "Pinned" })).toHaveTextContent("Pinned revenue");
    const todayRegion = screen.getByRole("region", { name: "Today" });
    expect(todayRegion).toHaveTextContent("Today PO check");
    expect(todayRegion.textContent?.indexOf("Today PO check")).toBeLessThan(todayRegion.textContent?.indexOf("Morning queue") ?? -1);
    expect(screen.getByRole("region", { name: "Yesterday" })).toHaveTextContent("Yesterday supplier check");
    expect(screen.getByRole("region", { name: "Previous 7 days" })).toHaveTextContent("Check last week margins");
    expect(screen.getByRole("region", { name: "Previous 30 days" })).toHaveTextContent("Monthly AR summary");
    expect(screen.getByRole("region", { name: "Older" })).toHaveTextContent("Q1 archive");
    expect(screen.getAllByTestId("chat-row-age").length).toBeGreaterThanOrEqual(7);

    // Active row uses crisp bg-row-on highlight with high-contrast text-fg
    const activeRow = screen.getByText("Today PO check").closest("li");
    expect(activeRow?.className).toContain("bg-row-on");
    expect(activeRow?.className).toContain("font-medium");

    // Hover quick Pin and Hide actions work directly
    api.updateAskChat.mockResolvedValue(bucketed);
    await userEvent.click(screen.getByLabelText("Pin Today PO check"));
    expect(api.updateAskChat).toHaveBeenCalledWith("today-1", { pinned: true });
    await userEvent.click(screen.getByLabelText("Hide Check last week margins"));
    expect(screen.getByRole("group", { name: "Delete this chat" })).toBeInTheDocument();
  });
});
