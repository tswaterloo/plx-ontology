import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const feeds = vi.hoisted(() => ({ get: vi.fn() }));
vi.mock("@/lib/api", async (orig) => ({ ...(await orig<typeof import("@/lib/api")>()), getFeeds: feeds.get }));

import { forgetFeeds } from "@/lib/feeds";
import { FeedNote } from "./feed-note";

describe("a feed note", () => {
  beforeEach(() => forgetFeeds());
  it("says a stopped job is not 'nothing to do', with the check's own words", async () => {
    feeds.get.mockResolvedValue({ checked_at: "x", feeds: [{ key: "reply_drafts", label: "Reply drafts", state: "not_running", why: "Reply drafts are switched off." }] });
    render(<FeedNote feed="reply_drafts" />);
    const note = await screen.findByTestId("feed-note-reply_drafts");
    expect(note.textContent).toMatch(/^Reply drafts are switched off\. An empty list here/);
    expect(note.textContent).toMatch(/not "nothing to do"/);
  });

  it("says unchecked when the health checks have not run, and nothing when the job runs", async () => {
    feeds.get.mockResolvedValue({ checked_at: null, feeds: [{ key: "late_pos", label: "Late purchase orders and alerts", state: "unknown", why: "" }, { key: "autopilot", label: "Autopilot entries", state: "running", why: "" }] });
    const { unmount } = render(<FeedNote feed="late_pos" />);
    expect((await screen.findByTestId("feed-note-late_pos")).textContent).toMatch(/^Not checked: whether late purchase orders and alerts are being made/);
    unmount();
    render(<FeedNote feed="autopilot" />);
    await Promise.resolve();
    expect(screen.queryByTestId("feed-note-autopilot")).toBeNull();
  });
});
