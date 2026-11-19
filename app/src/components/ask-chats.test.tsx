import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { AskChats } from "@/lib/types";

const api = vi.hoisted(() => ({ getAskChats: vi.fn(), updateAskChat: vi.fn() }));
vi.mock("@/lib/api", async (orig) => ({ ...(await orig<object>()), ...api }));

import { AskChatList } from "./ask-chats";

const hoursAgo = (h: number) => new Date(Date.now() - h * 3_600_000).toISOString();
const LIST: AskChats = {
  available: true,
  chats: [
    { conversation_id: "c2", title: "PO 90412 chase", named: true, pinned: true, questions: 3, last_at: hoursAgo(30) },
    { conversation_id: "c1", title: "Which orders are late?", named: false, pinned: false, questions: 1, last_at: hoursAgo(1) },
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
});
