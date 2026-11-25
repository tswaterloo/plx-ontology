import { act, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { getAskChats, getTeam, keepInsights, pageReadsOpen, readPath, streamInsights } from "./api";
import { noteAnswerHeaders } from "./read-store";
import { resetReady, watchReady } from "./ready";
import { resetSessionCaches } from "./session-cache";
import { forgetKeptReads, readAhead, useRead } from "./use-read";
import { setViewingAs } from "./view-as";

const tick = (ms = 20) => new Promise((r) => setTimeout(r, ms));

afterEach(() => {
  setViewingAs(null);
  resetSessionCaches();
  forgetKeptReads();
  vi.unstubAllGlobals();
  delete document.body.dataset.ready;
});

function TeamLikePage() {
  const { data } = useRead<{ state: string }>("/api/team", (s) => getTeam(s) as Promise<{ state: string }>);
  return <p>{data ? `team:${data.state}` : "loading"}</p>;
}

describe("second-visit hot-tier ready timing (SPEED-SPECS §10.9)", () => {
  it("renders synchronously from hot tier and marks body ready while background revalidation is in flight", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ state: "v1" }), { status: 200 })));
    const first = render(<TeamLikePage />);
    expect(await screen.findByText("team:v1")).toBeInTheDocument();
    first.unmount();

    let release!: () => void;
    const gate = new Promise<void>((r) => (release = r));
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        await gate;
        return new Response(JSON.stringify({ state: "v2" }), { status: 200 });
      }),
    );

    const stop = watchReady();
    resetReady();
    render(<TeamLikePage />);
    // Hot-tier hit paints synchronously on first render
    expect(screen.getByText("team:v1")).toBeInTheDocument();
    await tick();
    // Revalidation is quiet: in-flight page read count stays 0 and page is marked ready
    expect(pageReadsOpen()).toBe(0);
    expect(document.body.dataset.ready).toBe("1");

    await act(async () => {
      release();
      await tick();
    });
    expect(screen.getByText("team:v2")).toBeInTheDocument();
    stop();
  });

  it("readAhead keeps pageReadsOpen at 0 and sends X-PLX-Ahead when using readPath(..., true)", async () => {
    let release!: () => void;
    const gate = new Promise<void>((r) => (release = r));
    const seenHeaders: Record<string, string>[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (_url: string, init?: RequestInit) => {
        seenHeaders.push((init?.headers as Record<string, string>) ?? {});
        await gate;
        return new Response(JSON.stringify({ state: "ok" }), { status: 200 });
      }),
    );

    readAhead("/api/team", (s) => readPath("/api/team", s, true));
    readAhead("/api/insights/planner_open", (s) => getTeam(s));
    await tick();
    expect(pageReadsOpen()).toBe(0);
    expect(seenHeaders[0]?.["X-PLX-Ahead"]).toBe("1");
    release();
    await tick();
  });

  it("getAskChats hot-tier hit returns immediately and revalidates quietly, and respects all wipe rules", async () => {
    const firstPayload = { available: true, chats: [{ conversation_id: "c1", title: "First", last_at: "2026-11-25T10:00:00Z", questions: 1, pinned: false, named: false }] };
    const secondPayload = { available: true, chats: [{ conversation_id: "c2", title: "Second", last_at: "2026-11-25T11:00:00Z", questions: 2, pinned: false, named: false }] };

    // First visit: blocking read populates hot tier
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(JSON.stringify(firstPayload), { status: 200, headers: { "X-PLX-Door": "d1" } })),
    );
    expect(await getAskChats("")).toEqual(firstPayload);

    // Second visit: returns cached list immediately, background revalidation sends X-PLX-Ahead and keeps pageReadsOpen === 0
    let release!: () => void;
    const gate = new Promise<void>((r) => (release = r));
    const headersSeen: Record<string, string>[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (_u: string, init?: RequestInit) => {
        headersSeen.push((init?.headers as Record<string, string>) ?? {});
        await gate;
        return new Response(JSON.stringify(secondPayload), { status: 200, headers: { "X-PLX-Door": "d1" } });
      }),
    );
    const cached = await getAskChats("");
    expect(cached).toEqual(firstPayload);
    await tick();
    expect(pageReadsOpen()).toBe(0);
    expect(headersSeen[0]?.["X-PLX-Ahead"]).toBe("1");
    release();
    await tick();

    // View-as isolation: switching view-as does NOT return owner's cached chats
    const viewAsFetch = vi.fn(async () => new Response(JSON.stringify({ available: true, chats: [] }), { status: 200 }));
    vi.stubGlobal("fetch", viewAsFetch);
    setViewingAs({ key: "0123456789abcdef0123", name: "Someone" });
    expect(await getAskChats("")).toEqual({ available: true, chats: [] });
    expect(viewAsFetch).toHaveBeenCalledTimes(1);
    setViewingAs(null);

    // X-PLX-Door change wipes cached chats
    noteAnswerHeaders(new Headers({ "X-PLX-Door": "d2" }));
    const afterDoorFetch = vi.fn(async () => new Response(JSON.stringify({ available: true, chats: [] }), { status: 200, headers: { "X-PLX-Door": "d2" } }));
    vi.stubGlobal("fetch", afterDoorFetch);
    expect(await getAskChats("")).toEqual({ available: true, chats: [] });
    expect(afterDoorFetch).toHaveBeenCalledTimes(1);

    // Refused session wipes cached chats
    noteAnswerHeaders(new Headers({ "X-PLX-Session": "refused" }));
    const afterRefusedFetch = vi.fn(async () => new Response(JSON.stringify({ available: true, chats: [] }), { status: 200 }));
    vi.stubGlobal("fetch", afterRefusedFetch);
    expect(await getAskChats("")).toEqual({ available: true, chats: [] });
    expect(afterRefusedFetch).toHaveBeenCalledTimes(1);
  });

  it("streamInsights counts as a page read on first visit, and runs quietly on second visit when keptInsights is populated", async () => {
    let release1!: () => void;
    const gate1 = new Promise<void>((r) => (release1 = r));
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        await gate1;
        return new Response('{"list":{"items":[]}}\n', { status: 200 });
      }),
    );
    const p1 = streamInsights(() => undefined);
    await tick();
    expect(pageReadsOpen()).toBe(1);
    release1();
    await p1;

    // Populate keptInsights (as InsightsPage does after first visit)
    keepInsights({ items: [], scope: "everyone", saving: false }, {});

    let release2!: () => void;
    const gate2 = new Promise<void>((r) => (release2 = r));
    let aheadHeader: string | undefined;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (_u: string, init?: RequestInit) => {
        aheadHeader = (init?.headers as Record<string, string>)?.["X-PLX-Ahead"];
        await gate2;
        return new Response('{"list":{"items":[]}}\n', { status: 200 });
      }),
    );
    const p2 = streamInsights(() => undefined);
    await tick();
    expect(pageReadsOpen()).toBe(0);
    expect(aheadHeader).toBe("1");
    release2();
    await p2;
  });
});
