import { afterEach, describe, expect, it, vi } from "vitest";
import { forgetMemory } from "./api";
import { noteOwnWrite, writeKeepsReads } from "./read-store";
import { forgetKeptReads, keepRead, keptRead, readAhead } from "./use-read";

afterEach(() => {
  vi.unstubAllGlobals();
  forgetKeptReads();
});

describe("your own write and the hot tier", () => {
  it("an answer kept before your change is gone after it; a write that changes nothing shown leaves it", () => {
    keepRead("/api/memory", { items: [1] });
    keepRead("/api/approvals", { total: 3 });
    noteOwnWrite("/api/ask/chats/abc"); // a question asked: nothing a list shows
    expect(keptRead("/api/memory")).not.toBeNull();
    noteOwnWrite("/api/memory/5/done");
    expect(keptRead("/api/memory")).toBeNull();
    expect(keptRead("/api/approvals")).toBeNull(); // any other page too: a change reaches more than its own list
  });

  it("an unknown write clears (fails closed), and the keep list names only prefixes of whole segments", () => {
    keepRead("/api/trust", { a: 1 });
    noteOwnWrite("/api/something-new");
    expect(keptRead("/api/trust")).toBeNull();
    expect(writeKeepsReads("/api/askew")).toBe(false);
    expect(writeKeepsReads("/api/ask")).toBe(true);
  });

  it("a write sent through the API client clears what was kept", async () => {
    keepRead("/api/memory", { items: [1] });
    vi.stubGlobal("fetch", vi.fn(async () => new Response("{}", { status: 200 })));
    await forgetMemory(5).catch(() => undefined);
    expect(keptRead("/api/memory")).toBeNull();
  });

  it("a read already out when the change was sent does not keep its older answer", async () => {
    let answer!: (v: unknown) => void;
    readAhead("/api/memory", () => new Promise((r) => (answer = r)));
    noteOwnWrite("/api/memory/5/done"); // the change goes out while the read is in flight
    answer({ items: ["older"] });
    await new Promise((r) => setTimeout(r, 0));
    expect(keptRead("/api/memory")).toBeNull();
  });
});
