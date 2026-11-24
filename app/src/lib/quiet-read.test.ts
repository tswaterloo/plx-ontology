import { afterEach, describe, expect, it, vi } from "vitest";
import { getApprovals, getUpcomingMeetings, pageReadsOpen } from "./api";
import { quietly } from "./quiet";

afterEach(() => vi.unstubAllGlobals());

// a read behind an answer already drawn must not count as the page's own read, also when the
// read goes through getRead's polling (an await before the request ended quietly's scope)
describe("a quiet re-read through getRead", () => {
  async function open(quiet: boolean, read: () => Promise<unknown> = () => getUpcomingMeetings()): Promise<number> {
    let release!: () => void;
    const gate = new Promise<void>((r) => (release = r));
    vi.stubGlobal("fetch", vi.fn(async () => (await gate, new Response(JSON.stringify({ meetings: [] }), { status: 200 }))));
    const p = quiet ? quietly(read) : read();
    await new Promise((r) => setTimeout(r, 20));
    const n = pageReadsOpen();
    release();
    await p;
    return n;
  }
  it("is not a page read; the same read outside quietly is", async () => {
    expect(await open(true)).toBe(0);
    expect(await open(false)).toBe(1);
  });
  it("also a read Home's bundle names (approvals), when no bundle is under way", async () => {
    expect(await open(true, () => getApprovals())).toBe(0);
    expect(await open(false, () => getApprovals())).toBe(1);
  });
});
